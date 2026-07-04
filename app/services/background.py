import asyncio
from datetime import UTC, datetime, timedelta
import json
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.platforms.ggsel import ggsel
from app.clients.suppliers.smm_panel import smm_panel
from app.clients.telegram import formatting as fmt
from app.clients.telegram.client import telegram
from app.core.config.settings import settings
from app.db import repository as repo
from app.db.session import async_session

# ссылки на фоновые задачи, чтобы их не собрал GC
_tasks: set[asyncio.Task] = set()


def _track(task: asyncio.Task) -> None:
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _status_url(code: str) -> str:
    return f"{settings.BASE_PUBLIC_URL}/status?code={code}"


def _is_status_ok(status_obj: Any) -> bool:
    """True → заказ в порядке (тихое уведомление); False → ошибка/отмена/partial (со звуком).

    Кривой/неожиданный статус трактуем как «громко» — лучше лишний звук, чем пропустить ошибку.
    """
    if not status_obj:
        return True
    try:
        if fmt.supplier_canceled(status_obj):
            return False
        if isinstance(status_obj, dict):
            st = str(status_obj.get("status", "")).strip().lower()
            is_error = bool(status_obj.get("error")) or ("error" in st)
            if is_error or st in {"canceled", "cancelled", "refunded", "refund", "failed", "error", "partial", "paused"}:
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
) -> None:
    _track(
        asyncio.create_task(
            _status_check(platform_name, unique_code, order_id, purchase, email, goods_name, options, supplier_resp)
        )
    )


async def _status_check(
    platform_name: str,
    unique_code: str,
    order_id: str,
    purchase: dict[str, Any],
    email: str,
    goods_name: str,
    options: list,
    supplier_resp: dict[str, Any],
) -> None:
    try:
        await asyncio.sleep(settings.STATUS_CHECK_DELAY_SECONDS)
        status = await smm_panel.get_supplier_status(str(order_id))
        async with async_session() as session:
            await repo.update_supplier_by_ucode(session, unique_code, json.dumps(status, ensure_ascii=False))
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
            if await repo.try_mark_notified(session, unique_code, "success" if silent else "fail"):
                await telegram.send_message(msg, silent=silent)
    except Exception as e:
        logger.warning(f"[BG] проверка статуса не удалась code={unique_code} order={order_id}: {e}")


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

    max_seen = last_id or 0
    to_send: list[tuple[int, dict]] = []
    for m in msgs_sorted:
        mid = int(m.get("id") or 0)
        if last_id is not None and mid <= last_id:
            continue
        max_seen = max(max_seen, mid)
        if int(m.get("buyer") or 0) == 1 and not int(m.get("deleted") or 0):
            to_send.append((mid, m))

    # При первой встрече чата с bootstrap silent проглатываем историю без форварда.
    if first_seen and settings.GGSEL_CHAT_BOOTSTRAP_SILENT:
        await repo.ggsel_chat_set_last_msg_id(session, chat_id, max_seen)
        logger.info(f"[GGSEL-CHAT] bootstrap chat_id={chat_id} last_msg_id={max_seen} (пропущено {len(to_send)} сообщений)")
        return

    email = chat.get("email") or chat.get("buyer_email") or "—"
    first_failed: int | None = None
    for mid, m in to_send:
        text = str(m.get("message") or "")
        preview = text if len(text) <= 120 else text[:120] + "…"
        logger.info(f"[GGSEL-CHAT] email={email} msg={mid} текст={preview!r}")
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
