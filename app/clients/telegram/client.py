from loguru import logger

from app.clients.base import BaseApi
from app.core.config.settings import settings

_SEND_URL = f"https://api.telegram.org/bot{settings.TG_BOT_TOKEN.get_secret_value()}/sendMessage"


def _proxy() -> str | None:
    if settings.TG_PROXY_ENABLED and settings.TG_PROXY_URL is not None:
        return settings.TG_PROXY_URL.get_secret_value()
    return None


class TelegramApi(BaseApi):
    async def send_message(self, text: str, silent: bool = False) -> bool:
        payload = {
            "chat_id": settings.TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": bool(silent),
        }
        try:
            await self.request("POST", _SEND_URL, json_data=payload)
            return True
        except Exception as e:
            logger.exception(f"[TG] ✖ ошибка отправки chat_id={settings.TG_CHAT_ID} silent={silent} text={text!r}: {e}")
            return False


telegram = TelegramApi(proxy=_proxy())
