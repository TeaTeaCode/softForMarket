import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

from app.clients.suppliers.smm_panel import smm_panel
from app.clients.telegram.client import telegram
from app.core.config.config import config
from app.core.config.settings import settings
from app.core.logging import setup_logging
from app.db.session import async_session, engine
from app.services import background
from app.services.outbox.smm_panel import SmmPanelOutboxDeliverer
from app.services.outbox.worker import OutboxWorker, OutboxWorkerConfig
from app.services.worker_signals import install_shutdown_handlers


async def _cleanup(name: str, action: Callable[[], Awaitable[None]]) -> None:
    try:
        await action()
    except Exception as error:
        logger.opt(exception=error).error(f"[OUTBOX] ошибка при завершении {name}: {error}")


async def _close_notification_clients() -> None:
    results = await asyncio.gather(smm_panel.close(), telegram.close(), return_exceptions=True)
    for client, result in zip((smm_panel, telegram), results, strict=True):
        if isinstance(result, BaseException):
            logger.opt(exception=result).error(f"[OUTBOX] не удалось закрыть клиент {type(client).__name__}: {result}")


async def _main(stop_event: asyncio.Event | None = None) -> None:
    setup_logging(config.logging.outbox_worker_file)
    logger.info("[OUTBOX] старт отдельного процесса доставки")
    deliverer: SmmPanelOutboxDeliverer | None = None
    worker: OutboxWorker | None = None
    try:
        shutdown_event = stop_event or asyncio.Event()
        if stop_event is None:
            install_shutdown_handlers(shutdown_event)
        deliverer = SmmPanelOutboxDeliverer()
        worker = OutboxWorker(OutboxWorkerConfig.from_settings(settings), async_session, deliverer)
        worker.start()
        await shutdown_event.wait()
    finally:
        if worker is not None:
            await _cleanup("worker", worker.stop)
        await _cleanup("фоновых проверок статуса", background.stop_all)
        await _cleanup("клиентов уведомлений", _close_notification_clients)
        if deliverer is not None:
            await _cleanup("HTTP-клиента доставки", deliverer.close)
        await _cleanup("DB engine", engine.dispose)


if __name__ == "__main__":
    asyncio.run(_main())
