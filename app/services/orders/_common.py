import json
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.telegram import formatting as fmt
from app.clients.telegram.client import telegram
from app.core.config.settings import settings
from app.db import repository as repo
from app.services.links import INVALID_TG_LINK_MSG


def status_url(code: str) -> str:
    return f"{settings.BASE_PUBLIC_URL}/status?code={code}"


async def _notify(session: AsyncSession, text: str, silent: bool = False, dedupe: tuple[str, str] | None = None) -> None:
    """Шлёт уведомление с дедупликацией через таблицу notified."""
    if dedupe:
        code, kind = dedupe
        if not await repo.try_mark_notified(session, code, kind):
            logger.info(f"[TG] пропуск дубля уведомления code={code} kind={kind}")
            return
    ok = await telegram.send_message(text, silent=silent)
    logger.info(f"[TG] {'отправлено' if ok else 'НЕ ОТПРАВЛЕНО'} silent={silent} dedupe={dedupe} text={text!r}")


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
    supplier: str = "smm_panel",
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
        "supplier": supplier if order_id else None,
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
