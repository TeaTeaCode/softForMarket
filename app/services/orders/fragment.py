from collections.abc import Awaitable, Callable
import json
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.suppliers.fragment import PREMIUM_MONTHS, Source, fragment
from app.clients.telegram import formatting as fmt
from app.db import repository as repo
from app.services import background
from app.services.links import INVALID_TG_USERNAME_MSG, extract_months, extract_username_option
from app.services.orders._common import _notify, _purchase_fields, _purchase_row, status_url

# тип товара → как оформить заказ у Fragment
_CREATE_ORDER: dict[str, Callable[[str, int, int | None, Source], Awaitable[str]]] = {
    "stars": lambda username, quantity, months, source: fragment.create_stars_order(username, quantity, source),
    "premium": lambda username, quantity, months, source: fragment.create_premium_order(username, int(months or 0), source),
}

_SOURCE_LABELS: dict[Source, tuple[str, str]] = {
    Source.ggsel: ("ggsel", "GGSEL"),
    Source.digiseller: ("digiseller", "PLATI"),
}


async def process_fragment(
    session: AsyncSession,
    kind: str,
    unique_code: str,
    data: dict[str, Any],
    inv: Any,
    goods_id: str,
    goods_name: str,
    email: str,
    link: Any,
    quantity: int,
    options: list,
    source: Source,
) -> None:
    """Оформление Stars/Premium через Fragment. Заказ асинхронный: сохраняем task_id."""
    months = extract_months(options) if kind == "premium" else None
    platform, label = _SOURCE_LABELS[source]

    async def fail(message: str, status: str = "ERROR") -> None:
        logger.error(f"[FRAGMENT] ✖ отказ code={unique_code} kind={kind} status={status}: {message}")
        row = _purchase_row(
            platform, unique_code, inv, goods_id, data, email, link, months, quantity, None, status, supplier="fragment"
        )
        row["supplier_status"] = json.dumps({"status": "error", "message": message}, ensure_ascii=False)
        await repo.insert_purchase(session, row)
        await _notify(
            session,
            fmt.fmt_failure_msg(
                label,
                unique_code,
                _purchase_fields(data, goods_id),
                email,
                goods_name,
                options,
                message,
                status_url(unique_code),
                link=str(link or "—"),
                days=months,
                quantity=quantity,
            ),
            dedupe=(unique_code, "fail"),
        )

    username, username_err = extract_username_option(options)
    if username_err or not username:
        await fail(INVALID_TG_USERNAME_MSG, "ERROR_INVALID_USERNAME")
        return

    if kind == "premium" and not months:
        await fail(f"Не удалось определить срок подписки (goods_id={goods_id})")
        return

    if kind == "premium" and months not in PREMIUM_MONTHS:
        await fail(f"Fragment принимает подписку только на {sorted(PREMIUM_MONTHS)} мес., в заказе: {months}")
        return

    logger.info(f"[FRAGMENT] проверка получателя code={unique_code} username=@{username} kind={kind} months={months}")
    if not await fragment.check_username(username):
        await fail(f"Fragment: получатель @{username} не найден")
        return

    task_id = await _CREATE_ORDER[kind](username, quantity, months, source)

    await repo.insert_purchase(
        session,
        _purchase_row(
            platform,
            unique_code,
            inv,
            goods_id,
            data,
            email,
            f"https://t.me/{username}",
            months,
            quantity,
            task_id,
            "SUPPLIER_ACCEPTED",
            supplier="fragment",
        ),
    )
    logger.success(
        f"[FRAGMENT] ✔ заказ оформлен code={unique_code} kind={kind} username=@{username} "
        f"qty={quantity} months={months} task_id={task_id}"
    )
    background.schedule_status_check(
        label,
        unique_code,
        task_id,
        _purchase_fields(data, goods_id),
        email,
        goods_name,
        options,
        {"order": task_id},
        supplier="fragment",
    )
