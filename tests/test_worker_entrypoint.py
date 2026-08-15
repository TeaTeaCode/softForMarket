import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app import worker as worker_module


async def test_background_worker_does_not_start_outbox_delivery():
    stop_event = asyncio.Event()
    stop_event.set()
    fake_engine = Mock(dispose=AsyncMock())

    with (
        patch.object(worker_module, "setup_logging"),
        patch.object(worker_module.background, "start_chat_poller") as start_chat,
        patch.object(worker_module.background, "start_order_poller") as start_orders,
        patch.object(worker_module.background, "stop_all", AsyncMock()) as stop_background,
        patch.object(worker_module, "_close_worker_clients", AsyncMock()) as close_clients,
        patch.object(worker_module, "engine", fake_engine),
    ):
        await worker_module._main(stop_event)

    start_chat.assert_called_once_with()
    start_orders.assert_called_once_with()
    stop_background.assert_awaited_once_with()
    close_clients.assert_awaited_once_with()
    fake_engine.dispose.assert_awaited_once_with()
