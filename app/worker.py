import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

from app.clients.platforms.ggsel import ggsel
from app.clients.suppliers.fragment import fragment
from app.clients.suppliers.smm_panel import smm_panel
from app.clients.suppliers.teateagram import teateagram
from app.clients.telegram.client import telegram
from app.core.config.config import config
from app.core.logging import setup_logging
from app.db.session import engine
from app.services import background
from app.services.worker_signals import install_shutdown_handlers


async def _close_worker_clients() -> None:
    clients = (ggsel, teateagram, smm_panel, fragment, telegram)
    results = await asyncio.gather(*(client.close() for client in clients), return_exceptions=True)
    for client, result in zip(clients, results, strict=True):
        if isinstance(result, BaseException):
            logger.opt(exception=result).error(f"[WORKER] не удалось закрыть клиент {type(client).__name__}: {result}")


async def _cleanup(name: str, action: Callable[[], Awaitable[None]]) -> None:
    try:
        await action()
    except Exception as error:
        logger.opt(exception=error).error(f"[WORKER] ошибка при завершении {name}: {error}")


async def _main(stop_event: asyncio.Event | None = None) -> None:
    setup_logging(config.logging.worker_file)
    logger.info("[WORKER] старт фонового процесса")
    try:
        shutdown_event = stop_event or asyncio.Event()
        if stop_event is None:
            install_shutdown_handlers(shutdown_event)
        background.start_chat_poller()
        background.start_order_poller()
        await shutdown_event.wait()
    finally:
        await _cleanup("фоновых задач", background.stop_all)
        await _cleanup("общих HTTP-клиентов", _close_worker_clients)
        await _cleanup("DB engine", engine.dispose)


if __name__ == "__main__":
    asyncio.run(_main())
