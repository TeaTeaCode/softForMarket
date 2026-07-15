import asyncio

from loguru import logger

from app.core.logging import setup_logging
from app.services import background


async def _main() -> None:
    setup_logging()
    logger.info("[WORKER] старт фонового процесса")
    background.start_chat_poller()
    background.start_order_poller()
    # держим процесс живым, пока крутятся фоновые задачи
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(_main())
