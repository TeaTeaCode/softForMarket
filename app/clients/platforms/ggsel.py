import asyncio
import base64
import hashlib
import json
import time
from typing import Any

import httpx
from loguru import logger

from app.clients.base import BaseApi
from app.core.config.settings import settings

GGSEL_BASE = "https://seller.ggsel.com"
GGSEL_LOGIN_URL = f"{GGSEL_BASE}/api_sellers/api/apilogin"
GGSEL_UNIQUE_URL_TPL = f"{GGSEL_BASE}/api_sellers/api/purchases/unique-code/{{code}}"
GGSEL_CHATS_URL = f"{GGSEL_BASE}/api_sellers/api/debates/v2/chats"
GGSEL_MESSAGES_URL = f"{GGSEL_BASE}/api_sellers/api/debates/v2"
GGSEL_LAST_SALES_URL = f"{GGSEL_BASE}/api_sellers/api/seller-last-sales"
GGSEL_PURCHASE_INFO_URL_TPL = f"{GGSEL_BASE}/api_sellers/api/purchase/info/{{inv}}"


def _proxy() -> str | None:
    if settings.TG_PROXY_ENABLED and settings.TG_PROXY_URL is not None:
        routes = [item for item in settings.TG_PROXY_URL.get_secret_value().split(",") if item]
        return routes[0] if routes else None
    return None


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "locale": "ru"}


def _token_expiry(token: str, now: float) -> float:
    """Когда обновить токен: за час до exp из JWT (живёт ~31 день), иначе через час."""
    try:
        payload = token.split(".")[1]
        exp = float(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return now + 3600
    return max(now, exp - 3600)


class GgselApi(BaseApi):
    def __init__(self, proxy: str | None = None) -> None:
        super().__init__(proxy=proxy)
        self._token: str | None = None
        self._expiry = 0.0
        self._lock = asyncio.Lock()

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return await super().request(*args, **kwargs)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                self._token = None  # протух или отозван — следующий вызов залогинится заново
            raise

    async def get_token(self) -> str:
        async with self._lock:
            now = time.time()
            if self._token and now < self._expiry:
                return self._token
            ts_ms = int(now * 1000)
            sign = hashlib.sha256((settings.GGSEL_API_KEY.get_secret_value() + str(ts_ms)).encode()).hexdigest()
            payload = {"timestamp": ts_ms, "sign": sign, "seller_id": settings.GGSEL_SELLER_ID}
            data = await self.request("POST", GGSEL_LOGIN_URL, json_data=payload)
            if data.get("retval") != 0 or "token" not in data:
                raise RuntimeError(f"Ошибка логина GGSEL: retval={data.get('retval')} {data.get('retdesc', '')}")
            token_value: str = data["token"]
            self._token, self._expiry = token_value, _token_expiry(token_value, now)
            return token_value

    async def get_purchase(self, code: str) -> dict[str, Any]:
        token = await self.get_token()
        url = GGSEL_UNIQUE_URL_TPL.format(code=code)
        result: dict[str, Any] = await self.request("GET", url, headers={"Authorization": f"Bearer {token}"})
        logger.info(f"[GGSEL] заказ {code} получен")
        return result

    async def get_last_sales(self, token: str, top: int) -> list[dict[str, Any]]:
        """Последние продажи, новые первыми: [{invoice_id, date, product: {id, name, …}}]."""
        params = {"token": token, "seller_id": settings.GGSEL_SELLER_ID, "top": top}
        data = await self.request("GET", GGSEL_LAST_SALES_URL, params=params, headers=_auth_headers(token))
        if data.get("retval") != 0:
            raise RuntimeError(f"GGSEL seller-last-sales: {data.get('retdesc')}")
        sales: list[dict[str, Any]] = data.get("sales") or []
        return sales

    async def get_purchase_info(self, token: str, inv: int) -> dict[str, Any]:
        """Заказ по номеру счёта. unique_code лежит в content.name."""
        url = GGSEL_PURCHASE_INFO_URL_TPL.format(inv=inv)
        data = await self.request("GET", url, params={"token": token}, headers=_auth_headers(token))
        if data.get("retval") != 0:
            raise RuntimeError(f"GGSEL purchase/info inv={inv}: {data.get('retdesc')}")
        content: dict[str, Any] = data.get("content") or {}
        return content

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
