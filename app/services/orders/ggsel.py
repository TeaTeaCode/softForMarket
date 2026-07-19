from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.platforms.ggsel import ggsel
from app.clients.suppliers.fragment import Source
from app.clients.suppliers.smm_panel import smm_panel
from app.clients.telegram import formatting as fmt
from app.core.config.config import config
from app.db import repository as repo
from app.services import background
from app.services.links import extract_days_and_link, normalize_tg_link, resolve_fragment_kind, resolve_service
from app.services.orders._common import (
    _notify,
    _purchase_fields,
    _purchase_row,
    _quantity,
    _save_error,
    _save_invalid_link,
    status_url,
)
from app.services.orders.fragment import process_fragment


async def process_ggsel(session: AsyncSession, unique_code: str) -> str:
    """Обработка заказа GGSEL. Возвращает unique_code для редиректа на /status."""
    logger.info(f"[GGSEL] ▶ старт обработки code={unique_code}")
    if await repo.get_by_unique_code(session, unique_code):
        logger.info(f"[GGSEL] ⏭ пропуск code={unique_code}: заказ уже в БД")
        return unique_code
    if not await repo.mark_inflight(session, unique_code):
        logger.info(f"[GGSEL] ⏭ пропуск code={unique_code}: обрабатывается параллельно")
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
        logger.info(
            f"[GGSEL] заказ разобран code={unique_code} inv={inv} goods_id={goods_id} «{goods_name}» "
            f"email={email} link={link} days={days} qty={quantity} options={options}"
        )

        if kind := resolve_fragment_kind("ggsel", goods_id):
            logger.info(f"[GGSEL] → маршрут Fragment ({kind}) code={unique_code}")
            await process_fragment(
                session, kind, unique_code, data, inv, goods_id, goods_name, email, link, quantity, options, Source.ggsel
            )
            return unique_code

        service_id = resolve_service("ggsel", goods_id, days)
        logger.info(f"[GGSEL] → маршрут SMM Panel code={unique_code} service={service_id}")
        if not service_id:
            err = f"Не удалось определить service_id (goods_id={goods_id}, days={days})"
            logger.error(f"[GGSEL] ✖ отказ code={unique_code}: {err}")
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
            logger.error(f"[GGSEL] ✖ отказ code={unique_code}: невалидная ссылка «{link}»")
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
        logger.success(
            f"[GGSEL] ✔ заказ оформлен code={unique_code} inv={inv} «{goods_name}» "
            f"service={service_id} link={norm_link} qty={quantity} order_id={order_id}"
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
