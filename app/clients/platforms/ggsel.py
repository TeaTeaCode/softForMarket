import hashlib
import time
from typing import Any

from loguru import logger

from app.clients.base import BaseApi
from app.core.config.settings import settings

GGSEL_BASE = "https://seller.ggsel.com"
GGSEL_LOGIN_URL = f"{GGSEL_BASE}/api_sellers/api/apilogin"
GGSEL_UNIQUE_URL_TPL = f"{GGSEL_BASE}/api_sellers/api/purchases/unique-code/{{code}}"
GGSEL_CHATS_URL = f"{GGSEL_BASE}/api_sellers/api/debates/v2/chats"
GGSEL_MESSAGES_URL = f"{GGSEL_BASE}/api_sellers/api/debates/v2"


def _proxy() -> str | None:
    if settings.TG_PROXY_ENABLED and settings.TG_PROXY_URL is not None:
        routes = [item for item in settings.TG_PROXY_URL.get_secret_value().split(",") if item]
        return routes[0] if routes else None
    return None


class GgselApi(BaseApi):
    async def get_token(self) -> str:
        ts_ms = int(time.time() * 1000)
        sign = hashlib.sha256((settings.GGSEL_API_KEY.get_secret_value() + str(ts_ms)).encode()).hexdigest()
        payload = {"timestamp": ts_ms, "sign": sign, "seller_id": settings.GGSEL_SELLER_ID}
        data = await self.request("POST", GGSEL_LOGIN_URL, json_data=payload)
        if data.get("retval") != 0 or "token" not in data:
            raise RuntimeError(f"Ошибка логина GGSEL: {data}")
        token_value: str = data["token"]
        return token_value

    async def get_purchase(self, code: str) -> dict[str, Any]:
        token = await self.get_token()
        url = GGSEL_UNIQUE_URL_TPL.format(code=code)
        result: dict[str, Any] = await self.request("GET", url, headers={"Authorization": f"Bearer {token}"})
        logger.info(f"[GGSEL] заказ {code} получен")
        return result

    async def get_chats(self, token: str, filter_new: int = 1, pagesize: int = 100, page: int = 1) -> dict[str, Any]:
        params: dict[str, Any] = {"token": token, "filter_new": filter_new, "pagesize": pagesize, "page": page}
        data = await self.request("GET", GGSEL_CHATS_URL, params=params)
        if isinstance(data, dict):
            return data
        return {"items": data if isinstance(data, list) else [], "cnt_pages": 1}

    async def get_chat_messages(self, token: str, chat_id: int, count: int = 100) -> list:
        # GGSEL называет идентификатор чата id_i
        params: dict[str, Any] = {"token": token, "id_i": chat_id, "count": count}
        data = await self.request("GET", GGSEL_MESSAGES_URL, params=params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("items") or data.get("messages") or []
        return []


ggsel = GgselApi(proxy=_proxy())
