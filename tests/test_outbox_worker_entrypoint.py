import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app import outbox_worker as outbox_worker_module
from app.core.config.settings import settings
from app.db.session import async_session


async def test_outbox_worker_entrypoint_starts_delivery_and_closes_resources():
    stop_event = asyncio.Event()
    stop_event.set()
    worker = Mock(start=Mock(), stop=AsyncMock())
    deliverer = Mock(close=AsyncMock())
    fake_engine = Mock(dispose=AsyncMock())

    with (
        patch.object(outbox_worker_module, "setup_logging"),
        patch.object(outbox_worker_module, "SmmPanelOutboxDeliverer", return_value=deliverer),
        patch.object(outbox_worker_module, "OutboxWorker", return_value=worker) as worker_cls,
        patch.object(outbox_worker_module.background, "stop_all", AsyncMock()) as stop_background,
        patch.object(outbox_worker_module, "_close_notification_clients", AsyncMock()) as close_clients,
        patch.object(outbox_worker_module, "engine", fake_engine),
    ):
        await outbox_worker_module._main(stop_event)

    args = worker_cls.call_args.args
    assert args[0].poll_interval == settings.OUTBOX_POLL_INTERVAL
    assert args[1] is async_session
    assert args[2] is deliverer
    worker.start.assert_called_once_with()
    worker.stop.assert_awaited_once_with()
    stop_background.assert_awaited_once_with()
    close_clients.assert_awaited_once_with()
    deliverer.close.assert_awaited_once_with()
    fake_engine.dispose.assert_awaited_once_with()


async def test_outbox_worker_cleanup_continues_after_stop_failure():
    stop_event = asyncio.Event()
    stop_event.set()
    worker = Mock(start=Mock(), stop=AsyncMock(side_effect=RuntimeError("stop failed")))
    deliverer = Mock(close=AsyncMock())
    fake_engine = Mock(dispose=AsyncMock())

    with (
        patch.object(outbox_worker_module, "setup_logging"),
        patch.object(outbox_worker_module, "SmmPanelOutboxDeliverer", return_value=deliverer),
        patch.object(outbox_worker_module, "OutboxWorker", return_value=worker),
        patch.object(outbox_worker_module.background, "stop_all", AsyncMock()) as stop_background,
        patch.object(outbox_worker_module, "_close_notification_clients", AsyncMock()) as close_clients,
        patch.object(outbox_worker_module, "engine", fake_engine),
    ):
        await outbox_worker_module._main(stop_event)

    stop_background.assert_awaited_once_with()
    close_clients.assert_awaited_once_with()
    deliverer.close.assert_awaited_once_with()
    fake_engine.dispose.assert_awaited_once_with()
