from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.platforms.digiseller import digiseller
from app.clients.suppliers.smm_panel import smm_panel
from app.clients.telegram import formatting as fmt
from app.core.config.config import config
from app.db import repository as repo
from app.services import background
from app.services.links import extract_days_and_link, normalize_tg_link, resolve_service
from app.services.orders._common import (
    _finish_inv,
    _int_or_none,
    _normalize_digi,
    _notify,
    _purchase_row,
    _quantity,
    _save_error,
    _save_invalid_link,
    status_url,
)


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
