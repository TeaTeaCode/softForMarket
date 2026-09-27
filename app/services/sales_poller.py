"""Поллер продаж GGSEL: запускает заказы, по которым покупатель не перешёл на редирект.

Заказ запускается тем же process_ggsel, что и редирект: unique_code берём из purchase/info (content.name),
поэтому защита от дублей (проверка кода в БД + mark_inflight) общая для обоих путей.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger

from app.clients.platforms.ggsel import ggsel
from app.clients.telegram.client import telegram
from app.clients.telegram.formatting import html_escape
from app.core.config.settings import settings
from app.db import repository as repo
from app.db.session import async_session
from app.services import background
from app.services.orders.ggsel import process_ggsel

# invoice_state: 1 создан, 2 отменён, 3 оплачен, 4 выполнен, 5 возвращён
_LAUNCHABLE_STATES = {3, 4}
_DEAD_STATES = {2, 5}

# счета, которые больше не трогаем до перезапуска: отменённые, возвращённые, без unique_code
_skipped: set[int] = set()


def start_sales_poller() -> None:
    background._track(asyncio.create_task(_sales_poller()))


async def _sales_poller() -> None:
    logger.info(f"[GGSEL-SALES] поллер запущен, интервал={settings.GGSEL_SALES_POLL_INTERVAL}с")
    await asyncio.sleep(10)
    while True:
        try:
            await poll_sales_once()
        except Exception as e:
            logger.warning(f"[GGSEL-SALES] ошибка цикла: {type(e).__name__}: {e}")
        await asyncio.sleep(settings.GGSEL_SALES_POLL_INTERVAL)


def _sale_in_window(sale: dict[str, Any], now: datetime) -> bool:
    """Продажа уже не слишком свежая (редирект успел бы сработать) и ещё не слишком старая."""
    try:
        sold_at = datetime.fromisoformat(str(sale.get("date")))
    except ValueError:
        return False
    if sold_at.tzinfo is None:
        sold_at = sold_at.replace(tzinfo=UTC)
    age = now - sold_at
    return timedelta(seconds=settings.GGSEL_SALES_MIN_AGE_SECONDS) <= age <= timedelta(hours=settings.GGSEL_SALES_MAX_AGE_HOURS)


async def poll_sales_once() -> None:
    token = await ggsel.get_token()
    sales = await ggsel.get_last_sales(token, settings.GGSEL_SALES_POLL_TOP)
    now = datetime.now(UTC)
    candidates = {
        int(sale["invoice_id"]): sale
        for sale in sales
        if sale.get("invoice_id") and int(sale["invoice_id"]) not in _skipped and _sale_in_window(sale, now)
    }
    if not candidates:
        return

    async with async_session() as session:
        known = await repo.get_known_invs(session, "ggsel", list(candidates))
    for inv, sale in candidates.items():
        if inv in known:
            continue
        try:
            await _launch(token, inv, sale)
        except Exception as e:
            logger.warning(f"[GGSEL-SALES] inv={inv}: {type(e).__name__}: {e}")


async def _launch(token: str, inv: int, sale: dict[str, Any]) -> None:
    info = await ggsel.get_purchase_info(token, inv)
    state = info.get("invoice_state")
    code = str(info.get("name") or "").strip()
    product = str((sale.get("product") or {}).get("name") or info.get("item_id") or "—")

    if state in _DEAD_STATES:
        logger.info(f"[GGSEL-SALES] ⏭ inv={inv} пропуск: invoice_state={state}")
        _skipped.add(inv)
        return
    if state not in _LAUNCHABLE_STATES:
        logger.info(f"[GGSEL-SALES] ⏭ inv={inv} ждём оплату: invoice_state={state}")
        return
    if not code:
        logger.warning(f"[GGSEL-SALES] ⏭ inv={inv} пропуск: в purchase/info нет unique_code (content.name)")
        _skipped.add(inv)
        return

    logger.info(f"[GGSEL-SALES] ▶ автозапуск inv={inv} code={code} «{product}»: покупатель не перешёл на редирект")
    async with async_session() as session:
        await process_ggsel(session, code)
        saved = await repo.get_by_unique_code(session, code)
    # нет строки — process_ggsel упал до записи, повторим в следующем цикле
    if saved is None:
        logger.warning(f"[GGSEL-SALES] inv={inv} code={code}: заказ не сохранён, повтор в следующем цикле")
        return
    await _notify(inv, code, product)


async def _notify(inv: int, code: str, product: str) -> None:
    text = (
        "⚙️ <b>Автозапуск GGSEL</b>: покупатель не перешёл на редирект\n"
        f"inv: <code>{inv}</code>\ncode: <code>{html_escape(code)}</code>\nтовар: {html_escape(product)}"
    )
    if not await telegram.send_message(text, silent=True):
        logger.warning(f"[GGSEL-SALES] уведомление не ушло inv={inv}")
