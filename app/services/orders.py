import json
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.platforms.digiseller import digiseller
from app.clients.platforms.ggsel import ggsel
from app.clients.suppliers.smm_panel import smm_panel
from app.clients.telegram import formatting as fmt
from app.clients.telegram.client import telegram
from app.core.config.config import config
from app.core.config.settings import settings
from app.db import repository as repo
from app.services import background
from app.services.links import INVALID_TG_LINK_MSG, extract_days_and_link, normalize_tg_link, resolve_service


def status_url(code: str) -> str:
    return f"{settings.BASE_PUBLIC_URL}/status?code={code}"


async def _notify(session: AsyncSession, text: str, *, silent: bool = False, dedupe: tuple[str, str] | None = None) -> None:
    """Шлёт уведомление с дедупликацией через таблицу notified."""
    if dedupe:
        code, kind = dedupe
        if not await repo.try_mark_notified(session, code, kind):
            logger.info(f"[TG] пропуск дубля уведомления code={code} kind={kind}")
            return
    await telegram.send_message(text, silent=silent)


async def process_ggsel(session: AsyncSession, unique_code: str) -> str:
    """Обработка заказа GGSEL. Возвращает unique_code для редиректа на /status."""
    if await repo.get_by_unique_code(session, unique_code):
        return unique_code
    if not await repo.mark_inflight(session, unique_code):
        return unique_code

    try:
        data = await ggsel.get_purchase(unique_code)
        if data.get("retval") != 0:
            raise RuntimeError(f"GGSEL error: {data.get('retdesc')}")

        inv = data.get("inv")
        goods_id = str(data.get("id_goods"))
        goods_name = config.services.goods_human.get(goods_id, goods_id)
        email = str(data.get("email") or "")
        options = data.get("options", []) or []
        days, link = extract_days_and_link(options)
        quantity = _quantity(data.get("cnt_goods"))

        service_id = resolve_service("ggsel", goods_id, days)
        if not service_id:
            err = f"Не удалось определить service_id (goods_id={goods_id}, days={days})"
            await _save_error(session, "ggsel", unique_code, data, goods_id, email, link, days, quantity, err)
            await _notify(
                session,
                fmt.fmt_failure_msg(
                    "GGSEL",
                    unique_code,
                    data,
                    email,
                    goods_name,
                    options,
                    err,
                    status_url(unique_code),
                    link=link,
                    days=days,
                    quantity=quantity,
                ),
                dedupe=(unique_code, "fail"),
            )
            return unique_code

        norm_link, link_err = normalize_tg_link(link)
        if link_err:
            await _save_invalid_link(
                session, "ggsel", "GGSEL", unique_code, data, goods_id, goods_name, email, link, days, quantity, options
            )
            return unique_code

        supplier = await smm_panel.create_supplier_order(service_id, str(norm_link), quantity)
        order_id = str(supplier.get("order"))
        purchase = _purchase_fields(data, goods_id)
        await repo.insert_purchase(
            session,
            _purchase_row(
                "ggsel", unique_code, inv, goods_id, data, email, norm_link, days, quantity, order_id, "SUPPLIER_ACCEPTED"
            ),
        )
        background.schedule_status_check("GGSEL", unique_code, order_id, purchase, email, goods_name, options, supplier)
        return unique_code
    except Exception as e:
        logger.exception(f"[GGSEL] ошибка обработки {unique_code}")
        await _notify(
            session,
            fmt.fmt_failure_msg("GGSEL", unique_code, {}, "", "—", [], str(e), status_url(unique_code)),
            dedupe=(unique_code, "fail"),
        )
        return unique_code
    finally:
        await repo.clear_inflight(session, unique_code)


async def process_digiseller(session: AsyncSession, unique_code: str) -> str:
    """Обработка заказа Digiseller/PLATI. Возвращает unique_code для редиректа."""
    if await repo.get_by_unique_code(session, unique_code):
        return unique_code

    try:
        data = await digiseller.get_purchase(unique_code)
        if int(data.get("retval", -999)) != 0:
            err = f"Digiseller retval={data.get('retval')}: {data.get('retdesc', '')}"
            await _notify(
                session,
                fmt.fmt_failure_msg("PLATI", unique_code, data, "", "—", [], err, status_url(unique_code)),
                dedupe=(unique_code, "fail"),
            )
            await _save_error(
                session, "digiseller", unique_code, data, str(data.get("id_goods") or "—"), "", None, None, 1, err
            )
            return unique_code

        purchase, options, email, goods_id = _normalize_digi(data)
        goods_name = config.services.goods_human.get(goods_id, goods_id)
        days, link = extract_days_and_link(options)
        quantity = _quantity(purchase.get("cnt_goods"))
        inv = _int_or_none(purchase.get("inv"))

        if inv is not None and await repo.is_processed(session, inv):
            return unique_code
        if inv is not None and not await repo.try_acquire_inflight_inv(session, inv):
            return unique_code

        service_id = resolve_service("plati", goods_id, days)
        if not service_id:
            err = f"Не удалось определить service_id (goods_id={goods_id}, days={days})"
            await _save_error(session, "digiseller", unique_code, purchase, goods_id, email, link, days, quantity, err)
            await _notify(
                session,
                fmt.fmt_failure_msg(
                    "PLATI",
                    unique_code,
                    purchase,
                    email,
                    goods_name,
                    options,
                    err,
                    status_url(unique_code),
                    link=link,
                    days=days,
                    quantity=quantity,
                ),
                dedupe=(unique_code, "fail"),
            )
            await _finish_inv(session, inv)
            return unique_code

        norm_link, link_err = normalize_tg_link(link)
        if link_err:
            await _save_invalid_link(
                session,
                "digiseller",
                "PLATI",
                unique_code,
                purchase,
                goods_id,
                goods_name,
                email,
                link,
                days,
                quantity,
                options,
            )
            await _finish_inv(session, inv)
            return unique_code

        supplier = await smm_panel.create_supplier_order(service_id, str(norm_link), quantity)
        order_id = supplier.get("order") or supplier.get("order_id") or supplier.get("id")
        if not order_id:
            err = "Поставщик не вернул order_id"
            await _save_error(session, "digiseller", unique_code, purchase, goods_id, email, norm_link, days, quantity, err)
            await _notify(
                session,
                fmt.fmt_failure_msg(
                    "PLATI",
                    unique_code,
                    purchase,
                    email,
                    goods_name,
                    options,
                    err,
                    status_url(unique_code),
                    link=norm_link,
                    days=days,
                    quantity=quantity,
                    service_id=service_id,
                ),
                dedupe=(unique_code, "fail"),
            )
            if inv is not None:
                await repo.release_inflight_inv(session, inv)
            return unique_code

        if inv is not None:
            await repo.save_supplier_order(session, inv, str(order_id))
        await repo.insert_purchase(
            session,
            _purchase_row(
                "digiseller",
                unique_code,
                purchase.get("inv"),
                goods_id,
                purchase,
                email,
                norm_link,
                days,
                quantity,
                str(order_id),
                "SUPPLIER_ACCEPTED",
            ),
        )
        await _finish_inv(session, inv)  # PLATI не должна ретраить после add

        purchase_meta = dict(purchase)
        purchase_meta["id_goods"] = goods_id
        background.schedule_status_check(
            "PLATI", unique_code, str(order_id), purchase_meta, email, goods_name, options, supplier
        )
        return unique_code
    except Exception as e:
        logger.exception(f"[PLATI] ошибка обработки {unique_code}")
        await _notify(
            session,
            fmt.fmt_failure_msg("PLATI", unique_code, {}, "", "—", [], str(e), status_url(unique_code)),
            dedupe=(unique_code, "fail"),
        )
        return unique_code


# ─── вспомогательное ──────────────────────────────────────────────────────────


def _quantity(raw: Any) -> int:
    try:
        return int(float(str(raw or "1").replace(",", ".")))
    except (TypeError, ValueError):
        return 1


def _int_or_none(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _normalize_digi(data: dict[str, Any]) -> tuple[dict[str, Any], list, str, str]:
    if "content" in data:
        purchase = dict(data["content"])
        purchase["inv"] = purchase.get("external_order_id") or data.get("inv") or "—"
        email = purchase.get("buyer_info", {}).get("email", "") or ""
    else:
        purchase = dict(data)
        email = purchase.get("email", "") or ""
    options = purchase.get("options", []) or []
    goods_id = str(purchase.get("id_goods") or purchase.get("item_id") or "—")
    return purchase, options, email, goods_id


def _purchase_fields(data: dict[str, Any], goods_id: str) -> dict[str, Any]:
    return {
        "inv": data.get("inv"),
        "id_goods": goods_id,
        "amount": data.get("amount"),
        "amount_usd": data.get("amount_usd"),
        "profit": data.get("profit"),
        "type_curr": data.get("type_curr"),
    }


def _purchase_row(
    platform: str,
    unique_code: str,
    inv: Any,
    goods_id: str,
    source: dict[str, Any],
    email: str,
    link: Any,
    days: int | None,
    quantity: int,
    order_id: str | None,
    status: str,
) -> dict[str, Any]:
    return {
        "platform": platform,
        "unique_code": unique_code,
        "inv": inv,
        "goods_id": goods_id,
        "amount": source.get("amount"),
        "amount_usd": source.get("amount_usd"),
        "profit": source.get("profit"),
        "currency": source.get("type_curr") or source.get("currency_type"),
        "email": email,
        "tg_link": str(link or "—"),
        "days": int(days) if days else None,
        "quantity": quantity,
        "supplier": "smm_panel" if order_id else None,
        "supplier_order_id": order_id,
        "supplier_status": None,
        "status": status,
        "created_at": repo.now_iso(),
    }


async def _save_error(
    session: AsyncSession,
    platform: str,
    unique_code: str,
    source: dict[str, Any],
    goods_id: str,
    email: str,
    link: Any,
    days: int | None,
    quantity: int,
    err: str,
) -> None:
    row = _purchase_row(platform, unique_code, source.get("inv"), goods_id, source, email, link, days, quantity, None, "ERROR")
    row["supplier_status"] = json.dumps({"status": "error", "message": err}, ensure_ascii=False)
    await repo.insert_purchase(session, row)


async def _save_invalid_link(
    session: AsyncSession,
    platform: str,
    platform_name: str,
    unique_code: str,
    source: dict[str, Any],
    goods_id: str,
    goods_name: str,
    email: str,
    link: Any,
    days: int | None,
    quantity: int,
    options: list,
) -> None:
    row = _purchase_row(
        platform, unique_code, source.get("inv"), goods_id, source, email, link, days, quantity, None, "ERROR_INVALID_LINK"
    )
    row["supplier_status"] = json.dumps({"status": "error", "message": INVALID_TG_LINK_MSG}, ensure_ascii=False)
    await repo.insert_purchase(session, row)
    purchase = {
        "inv": source.get("inv"),
        "id_goods": goods_id,
        "amount": source.get("amount"),
        "type_curr": source.get("type_curr"),
    }
    msg = fmt.fmt_failure_msg(
        platform_name,
        unique_code,
        purchase,
        email,
        goods_name,
        options,
        INVALID_TG_LINK_MSG,
        status_url(unique_code),
        link=str(link or "—"),
        days=days,
        quantity=quantity,
    )
    await _notify(session, msg, dedupe=(unique_code, "fail"))


async def _finish_inv(session: AsyncSession, inv: int | None) -> None:
    if inv is not None:
        await repo.mark_processed(session, inv)
        await repo.release_inflight_inv(session, inv)
