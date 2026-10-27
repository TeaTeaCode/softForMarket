from typing import Any

import httpx
from loguru import logger

from app.clients.base import RETRY_TOTAL, BaseApi
from app.core.config.settings import settings

TEA_API_BASE = "https://teateagram.com/api/v2"


class TeaTeaGramApi(BaseApi):
    async def create_supplier_order(self, service_id: int, link: str, quantity: int) -> dict[str, Any]:
        response = await self.create_supplier_order_response(service_id, link, quantity)
        data: dict[str, Any] = response.json()
        logger.info(f"[SUPPLIER] ответ socpanel на add: {data}")
        if "order" not in data:
            raise RuntimeError(f"Поставщик не принял заказ: {data}")
        return data

    async def create_supplier_order_response(
        self,
        service_id: int,
        link: str,
        quantity: int,
        *,
        retry_total: int = RETRY_TOTAL,
    ) -> httpx.Response:
        payload = {
            "key": settings.TEA_API_KEY.get_secret_value(),
            "action": "add",
            "service": service_id,
            "link": link,
            "quantity": quantity,
        }
        logger.info(f"[SUPPLIER] socpanel add service={service_id} link={link} qty={quantity}")
        response: httpx.Response = await self.request(
            "POST", TEA_API_BASE, data=payload, response_type="response", retry_total=retry_total
        )
        return response

    async def get_supplier_status(self, order_id: str) -> dict[str, Any]:
        payload = {"key": settings.TEA_API_KEY.get_secret_value(), "action": "status", "order": order_id}
        result: dict[str, Any] = await self.request("POST", TEA_API_BASE, data=payload)
        return result


teateagram = TeaTeaGramApi()
