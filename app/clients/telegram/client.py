import asyncio
from collections.abc import Sequence

import httpx
from loguru import logger

from app.clients.base import RETRY_TOTAL, TRANSPORT_RETRY_TOTAL, BaseApi
from app.core.config.settings import settings

_SEND_URL = f"https://api.telegram.org/bot{settings.TG_BOT_TOKEN.get_secret_value()}/sendMessage"


def _proxies() -> Sequence[str | None]:
    if settings.TG_PROXY_ENABLED and settings.TG_PROXY_URL is not None:
        return settings.TG_PROXY_URL.get_secret_value().split(",")
    return [None]


class TelegramApi:
    def __init__(self, proxies: Sequence[str | None]) -> None:
        routes = list(proxies) or [None]
        has_fallback = len(routes) > 1
        transport_retries = 0 if has_fallback else TRANSPORT_RETRY_TOTAL
        self._request_retries = 0 if has_fallback else RETRY_TOTAL
        self._clients = [BaseApi(proxy=proxy, transport_retry_total=transport_retries) for proxy in routes]
        self._next_client_index = 0

    async def close(self) -> None:
        results = await asyncio.gather(*(client.close() for client in self._clients), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                logger.opt(exception=result).error(f"[TG] ошибка закрытия HTTP-клиента: {result}")

    async def send_message(self, text: str, silent: bool = False) -> bool:
        payload = {
            "chat_id": settings.TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": bool(silent),
        }
        start_index = self._next_client_index
        self._next_client_index = (start_index + 1) % len(self._clients)
        last_error: Exception | None = None
        for offset in range(len(self._clients)):
            client_index = (start_index + offset) % len(self._clients)
            try:
                await self._clients[client_index].request(
                    "POST",
                    _SEND_URL,
                    json_data=payload,
                    retry_total=self._request_retries,
                )
                return True
            except httpx.TransportError as error:
                last_error = error
                logger.warning(
                    f"[TG] маршрут {client_index + 1}/{len(self._clients)} недоступен: {type(error).__name__}: {error}"
                )
            except Exception as error:
                logger.error(
                    f"[TG] ✖ ошибка отправки chat_id={settings.TG_CHAT_ID} silent={silent} "
                    f"text={text!r}: {type(error).__name__}: {error}"
                )
                return False
        logger.error(f"[TG] ✖ ошибка отправки chat_id={settings.TG_CHAT_ID} silent={silent} text={text!r}: {last_error}")
        return False


telegram = TelegramApi(_proxies())
