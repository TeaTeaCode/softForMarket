import asyncio
import signal
from unittest.mock import Mock, patch

from app.services.worker_signals import install_shutdown_handlers


async def test_sigterm_sets_worker_stop_event():
    handlers = {}
    loop = Mock()

    def capture_handler(sig, callback):
        handlers[sig] = callback

    loop.add_signal_handler.side_effect = capture_handler
    stop_event = asyncio.Event()

    with patch("app.services.worker_signals.asyncio.get_running_loop", return_value=loop):
        install_shutdown_handlers(stop_event)

    handlers[signal.SIGTERM]()
    assert stop_event.is_set()
    assert signal.SIGINT in handlers
