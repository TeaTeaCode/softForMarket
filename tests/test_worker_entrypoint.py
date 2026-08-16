import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app import worker as worker_module
from app.core.config.settings import settings
from app.db.session import async_session


async def test_worker_starts_pollers_and_outbox_delivery() -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    cleanup_order: list[str] = []
    outbox_worker = Mock(start=Mock(), stop=AsyncMock(side_effect=lambda: cleanup_order.append("outbox")))
    deliverer = Mock(close=AsyncMock(side_effect=lambda: cleanup_order.append("deliverer")))
    fake_engine = Mock(dispose=AsyncMock(side_effect=lambda: cleanup_order.append("engine")))

    with (
        patch.object(worker_module, "setup_logging"),
        patch.object(worker_module, "SmmPanelOutboxDeliverer", return_value=deliverer),
        patch.object(worker_module, "OutboxWorker", return_value=outbox_worker) as worker_cls,
        patch.object(worker_module.background, "start_chat_poller") as start_chat,
        patch.object(worker_module.background, "start_order_poller") as start_orders,
        patch.object(
            worker_module.background,
            "stop_all",
            AsyncMock(side_effect=lambda: cleanup_order.append("background")),
        ) as stop_background,
        patch.object(
            worker_module,
            "_close_worker_clients",
            AsyncMock(side_effect=lambda: cleanup_order.append("clients")),
        ) as close_clients,
        patch.object(worker_module, "engine", fake_engine),
    ):
        await worker_module._main(stop_event)

    args = worker_cls.call_args.args
    assert args[0].poll_interval == settings.OUTBOX_POLL_INTERVAL
    assert args[1] is async_session
    assert args[2] is deliverer
    outbox_worker.start.assert_called_once_with()
    start_chat.assert_called_once_with()
    start_orders.assert_called_once_with()
    outbox_worker.stop.assert_awaited_once_with()
    stop_background.assert_awaited_once_with()
    close_clients.assert_awaited_once_with()
    deliverer.close.assert_awaited_once_with()
    fake_engine.dispose.assert_awaited_once_with()
    assert cleanup_order == ["outbox", "background", "clients", "deliverer", "engine"]


async def test_worker_cleanup_continues_after_outbox_stop_failure() -> None:
    stop_event = asyncio.Event()
    stop_event.set()
    outbox_worker = Mock(start=Mock(), stop=AsyncMock(side_effect=RuntimeError("stop failed")))
    deliverer = Mock(close=AsyncMock())
    fake_engine = Mock(dispose=AsyncMock())

    with (
        patch.object(worker_module, "setup_logging"),
        patch.object(worker_module, "SmmPanelOutboxDeliverer", return_value=deliverer),
        patch.object(worker_module, "OutboxWorker", return_value=outbox_worker),
        patch.object(worker_module.background, "start_chat_poller"),
        patch.object(worker_module.background, "start_order_poller"),
        patch.object(worker_module.background, "stop_all", AsyncMock()) as stop_background,
        patch.object(worker_module, "_close_worker_clients", AsyncMock()) as close_clients,
        patch.object(worker_module, "engine", fake_engine),
    ):
        await worker_module._main(stop_event)

    stop_background.assert_awaited_once_with()
    close_clients.assert_awaited_once_with()
    deliverer.close.assert_awaited_once_with()
    fake_engine.dispose.assert_awaited_once_with()
