import asyncio
from datetime import UTC, datetime, timedelta
import json
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.platforms.ggsel import ggsel
from app.clients.suppliers.registry import fetch_status
from app.clients.telegram import formatting as fmt
from app.clients.telegram.client import telegram
from app.core.config.config import config
from app.core.config.settings import settings
from app.db import repository as repo
from app.db.session import async_session

# ссылки на фоновые задачи, чтобы их не собрал GC
_tasks: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def stop_all() -> None:
    tasks = tuple(_tasks)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.difference_update(tasks)


def _status_url(code: str) -> str:
    return f"{settings.BASE_PUBLIC_URL}/status?code={code}"


def _is_status_ok(status_obj: Any) -> bool:
    """True → тихое уведомление; False → со звуком. Неизвестный статус трактуем как громкий."""
    if not status_obj:
        return True
    try:
        if fmt.supplier_canceled(status_obj):
            return False
        if isinstance(status_obj, dict):
            st = str(status_obj.get("status", "")).strip().lower()
            is_error = bool(status_obj.get("error")) or ("error" in st)
            if is_error or st in {
                "canceled",
                "cancelled",
                "refunded",
                "refund",
                "failed",
                "error",
                "partial",
                "paused",
                "awaiting_balance",
            }:
                return False
    except (AttributeError, TypeError):
        return False
    return True


# ─── отложенная проверка статуса ──────────────────────────────────────────────


def schedule_status_check(
    platform_name: str,
    unique_code: str,
    order_id: str,
    purchase: dict[str, Any],
    email: str,
    goods_name: str,
    options: list,
    supplier_resp: dict[str, Any],
    supplier: str = "smm_panel",
) -> None:
    _track(
        asyncio.create_task(
            _status_check(platform_name, unique_code, order_id, purchase, email, goods_name, options, supplier_resp, supplier)
        )
    )


def schedule_outbox_status_check(
    unique_code: str,
    order_id: str,
    supplier_resp: dict[str, Any],
    supplier: str,
) -> None:
    """Restore notification context from Purchase after an Outbox delivery commits."""
    _track(asyncio.create_task(_outbox_status_check(unique_code, order_id, supplier_resp, supplier)))


async def _outbox_status_check(
    unique_code: str,
    order_id: str,
    supplier_resp: dict[str, Any],
    supplier: str,
) -> None:
    try:
        async with async_session() as session:
            row = await repo.get_by_unique_code(session, unique_code)
        if row is None:
            logger.error(f"[OUTBOX] Purchase не найдена для проверки статуса code={unique_code} order={order_id}")
            return
        goods_id = str(row.get("goods_id") or "")
        purchase = {
            "inv": row.get("inv"),
            "id_goods": goods_id,
            "amount": row.get("amount"),
            "amount_usd": row.get("amount_usd"),
            "profit": row.get("profit"),
            "type_curr": row.get("currency"),
        }
        options: list[dict[str, str]] = []
        link = str(row.get("tg_link") or "").strip()
        if link and link != "—":
            options.append({"name": "Ссылка", "value": link})
        if row.get("days") is not None:
            options.append({"name": "Количество дней", "value": str(row["days"])})
        await _status_check(
            "GGSEL" if row.get("platform") == "ggsel" else "PLATI",
            unique_code,
            order_id,
            purchase,
            str(row.get("email") or ""),
            config.services.goods_human.get(goods_id, goods_id),
            options,
            supplier_resp,
            supplier,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"[OUTBOX] не удалось запланировать проверку code={unique_code} order={order_id}: {e}")


async def _status_check(
    platform_name: str,
    unique_code: str,
    order_id: str,
    purchase: dict[str, Any],
    email: str,
    goods_name: str,
    options: list,
    supplier_resp: dict[str, Any],
    supplier: str = "smm_panel",
) -> None:
    try:
        await asyncio.sleep(settings.STATUS_CHECK_DELAY_SECONDS)
        status = await fetch_status(supplier, str(order_id))
        logger.info(f"[BG] проверка статуса code={unique_code} supplier={supplier} order={order_id} → {status}")
        async with async_session() as session:
            await repo.update_supplier_by_ucode(session, unique_code, json.dumps(status, ensure_ascii=False))
            # промежуточный статус шлём под своим ключом, иначе занял бы final и поллер смолчал бы на завершении
            kind = "final" if _is_final_status(status) else "started"
            msg = fmt.fmt_unified_order_msg(
                platform_name,
                unique_code,
                purchase,
                email,
                goods_name,
                options,
                supplier_resp,
                status,
                _status_url(unique_code),
            )
            silent = _is_status_ok(status)
            if await repo.try_mark_notified(session, unique_code, kind) and not await telegram.send_message(msg, silent=silent):
                await repo.unmark_notified(session, unique_code, kind)
                logger.warning(f"[BG] уведомление не ушло code={unique_code} kind={kind} — дошлёт поллер")
    except Exception as e:
        logger.warning(f"[BG] проверка статуса не удалась code={unique_code} order={order_id}: {e}")


# ─── поллер статусов заказов ──────────────────────────────────────────────────

# статусы поставщиков, после которых опрашивать больше нечего
FINAL_STATUSES = frozenset(
    {"success", "completed", "done", "finished", "failed", "canceled", "cancelled", "refunded", "refund"}
)


def start_order_poller() -> None:
    _track(asyncio.create_task(_order_poller()))


async def _order_poller() -> None:
    logger.info(f"[ORDER-POLL] поллер запущен, интервал={settings.ORDER_POLL_INTERVAL}с")
    await asyncio.sleep(5)
    while True:
        try:
            await _poll_orders_once()
        except Exception as e:
            logger.warning(f"[ORDER-POLL] ошибка цикла: {e}")
        await asyncio.sleep(settings.ORDER_POLL_INTERVAL)


def _is_final_status(status_obj: Any) -> bool:
    if fmt.supplier_canceled(status_obj):
        return True
    if isinstance(status_obj, dict):
        return str(status_obj.get("status", "")).strip().lower() in FINAL_STATUSES
    return False


def _is_order_stale(created_at: Any) -> bool:
    """Заказ старше ORDER_POLL_MAX_AGE_HOURS — перестаём опрашивать."""
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(str(created_at))
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return created < datetime.now(UTC) - timedelta(hours=settings.ORDER_POLL_MAX_AGE_HOURS)


async def _poll_orders_once() -> None:
    async with async_session() as session:
        rows = await repo.get_pending_orders(session, settings.ORDER_POLL_BATCH)

    if rows:
        logger.info(f"[ORDER-POLL] цикл: незавершённых заказов={len(rows)}")
    for row in rows:
        try:
            await _poll_order(row)
        except Exception as e:
            logger.warning(f"[ORDER-POLL] заказ code={row.get('unique_code')}: {type(e).__name__}: {e}")


async def _poll_order(row: dict[str, Any]) -> None:
    unique_code = str(row.get("unique_code") or "")
    order_id = str(row.get("supplier_order_id") or "")
    supplier = str(row.get("supplier") or "")
    if not unique_code or not order_id or not supplier:
        return

    saved = _safe_json(row.get("supplier_status"))
    # финал мог сохраниться, а уведомление — не уйти
    if _is_final_status(saved):
        async with async_session() as session:
            if await repo.is_notified(session, unique_code, "final"):
                # закрыт и уведомлён — снимаем с опроса, иначе крутится в батче до отсечки по возрасту
                await repo.mark_order_done(session, unique_code)
                logger.info(f"[ORDER-POLL] ✔ заказ закрыт, снят с опроса code={unique_code}")
                return
        logger.info(f"[ORDER-POLL] финал без уведомления code={unique_code} — досылаем")
    if _is_order_stale(row.get("created_at")):
        logger.info(f"[ORDER-POLL] ⏭ заказ протух code={unique_code} created_at={row.get('created_at')}")
        return

    status = await fetch_status(supplier, order_id)
    logger.info(f"[ORDER-POLL] статус code={unique_code} supplier={supplier} order={order_id} → {status}")

    async with async_session() as session:
        await repo.update_supplier_by_ucode(session, unique_code, json.dumps(status, ensure_ascii=False))
        if not _is_final_status(status):
            return

        silent = _is_status_ok(status)
        if not await repo.try_mark_notified(session, unique_code, "final"):
            await repo.mark_order_done(session, unique_code)
            return

        goods_id = str(row.get("goods_id") or "")
        msg = fmt.fmt_unified_order_msg(
            "GGSEL" if row.get("platform") == "ggsel" else "PLATI",
            unique_code,
            {"inv": row.get("inv"), "id_goods": goods_id, "amount": row.get("amount"), "type_curr": row.get("currency")},
            str(row.get("email") or ""),
            config.services.goods_human.get(goods_id, goods_id),
            [],
            {"order": order_id},
            status,
            _status_url(unique_code),
        )
        if not await telegram.send_message(msg, silent=silent):
            # иначе следующий цикл сочтёт заказ уведомлённым
            await repo.unmark_notified(session, unique_code, "final")
            logger.warning(f"[ORDER-POLL] уведомление не ушло code={unique_code} — повторим в следующем цикле")
            return
        await repo.mark_order_done(session, unique_code)
        logger.info(
            f"[ORDER-POLL] финал code={unique_code} status={status.get('status') if isinstance(status, dict) else status}"
        )


def _safe_json(raw: Any) -> Any:
    if not raw:
        return None
    if isinstance(raw, dict | list):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# ─── поллер чатов GGSEL ───────────────────────────────────────────────────────


def start_chat_poller() -> None:
    _track(asyncio.create_task(_chat_poller()))


async def _chat_poller() -> None:
    logger.info(f"[GGSEL-CHAT] поллер запущен, интервал={settings.GGSEL_CHAT_POLL_INTERVAL}с")
    await asyncio.sleep(3)
    while True:
        try:
            await _poll_once()
        except Exception as e:
            logger.warning(f"[GGSEL-CHAT] ошибка цикла: {e}")
        await asyncio.sleep(settings.GGSEL_CHAT_POLL_INTERVAL)


def _extract_chat_id(chat: dict[str, Any]) -> int | None:
    """Достаёт id чата из разных полей, которые встречаются в ответе GGSEL."""
    for key in ("id_i", "id", "id_debate", "id_ds"):
        val = chat.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                continue
    return None


def _is_chat_fresh(chat: dict[str, Any]) -> bool:
    raw = chat.get("last_message")
    if not raw:
        return False
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    threshold = datetime.now(UTC) - timedelta(days=settings.GGSEL_CHAT_FRESH_DAYS)
    return last >= threshold


def _msg_preview(msg: dict[str, Any]) -> str:
    text = str(msg.get("message") or "")
    return text if len(text) <= 120 else text[:200] + "…"


async def _poll_once() -> None:
    token = await ggsel.get_token()
    for page in range(1, settings.GGSEL_CHAT_MAX_PAGES + 1):
        data = await ggsel.get_chats(token, filter_new=0, page=page)
        items = data.get("items") or []
        if not items:
            break
        async with async_session() as session:
            for chat in items:
                if not _is_chat_fresh(chat):
                    continue
                try:
                    await _process_chat(session, token, chat)
                except Exception as e:
                    logger.warning(f"[GGSEL-CHAT] ошибка чата: {type(e).__name__}: {e}")


async def _process_chat(session: AsyncSession, token: str, chat: dict[str, Any]) -> None:
    chat_id = _extract_chat_id(chat)
    if not chat_id:
        return
    last_id = await repo.ggsel_chat_get_last_msg_id(session, chat_id)
    first_seen = last_id is None

    msgs = await ggsel.get_chat_messages(token, chat_id, count=100)
    if not msgs:
        if first_seen:
            await repo.ggsel_chat_set_last_msg_id(session, chat_id, 0)
        return

    msgs_sorted = sorted((m for m in msgs if m.get("id") is not None), key=lambda m: int(m.get("id") or 0))

    logger.info(f"[GGSEL-CHAT] chat_id={chat_id} получено сообщений={len(msgs_sorted)} last_msg_id={last_id}")

    max_seen = last_id or 0
    to_send: list[tuple[int, dict]] = []
    for m in msgs_sorted:
        mid = int(m.get("id") or 0)
        is_buyer = int(m.get("buyer") or 0) == 1
        is_deleted = bool(int(m.get("deleted") or 0))
        seen_before = last_id is not None and mid <= last_id
        if seen_before:
            skip = "уже видели"
        elif not is_buyer:
            skip = "от продавца"
        elif is_deleted:
            skip = "удалено"
        else:
            skip = ""
        author = "покупатель" if is_buyer else "продавец"
        suffix = f" ⏭ {skip}" if skip else " → в TG"
        logger.info(f"[GGSEL-CHAT] chat_id={chat_id} msg={mid} {author} текст={_msg_preview(m)!r}{suffix}")
        if seen_before:
            continue
        max_seen = max(max_seen, mid)
        if not skip:
            to_send.append((mid, m))

    if first_seen and settings.GGSEL_CHAT_BOOTSTRAP_SILENT:
        await repo.ggsel_chat_set_last_msg_id(session, chat_id, max_seen)
        logger.info(f"[GGSEL-CHAT] bootstrap chat_id={chat_id} last_msg_id={max_seen} (пропущено {len(to_send)} сообщений)")
        return

    email = chat.get("email") or chat.get("buyer_email") or "—"
    first_failed: int | None = None
    for mid, m in to_send:
        logger.info(f"[GGSEL-CHAT] отправка email={email} msg={mid} текст={_msg_preview(m)!r}")
        try:
            ok = await telegram.send_message(fmt.fmt_chat_msg_for_tg(chat, m, chat_id))
        except Exception as e:
            logger.warning(f"[GGSEL-CHAT] не отправлено chat_id={chat_id} msg={mid}: {e}")
            ok = False
        if not ok:
            first_failed = mid
            break

    # не двигаем last_id дальше неудачно отправленного сообщения
    if first_failed is None:
        new_last = max_seen
    else:
        candidates = [int(m.get("id") or 0) for m in msgs_sorted if (last_id or 0) < int(m.get("id") or 0) < first_failed]
        new_last = max(candidates) if candidates else (last_id or 0)

    if new_last > (last_id or 0):
        await repo.ggsel_chat_set_last_msg_id(session, chat_id, new_last)
