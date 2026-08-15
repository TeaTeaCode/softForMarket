from typing import Any

import httpx
from loguru import logger

from app.clients.base import RETRY_TOTAL, TRANSPORT_RETRY_TOTAL, BaseApi
from app.core.config.settings import settings


class SmmPanelApi(BaseApi):
    def __init__(self, *, transport_retry_total: int = TRANSPORT_RETRY_TOTAL) -> None:
        super().__init__(
            base_url=f"{settings.SMM_PANEL_BASE_URL.rstrip('/')}/api/v1/external",
            headers={"X-API-Key": settings.SMM_PANEL_API_KEY.get_secret_value()},
            transport_retry_total=transport_retry_total,
        )

    async def create_supplier_order(self, service_name: str, link: str, quantity: int) -> dict[str, Any]:
        response = await self.create_supplier_order_response(service_name, link, quantity)
        data: dict[str, Any] = response.json()
        logger.info(f"[SUPPLIER] ответ на add: {data}")
        if "id" not in data:
            raise RuntimeError(f"Поставщик не принял заказ: {data}")
        # "order" — для совместимости с кодом, ждущим формат TeaTeaGram
        data["order"] = data["id"]
        return data

    async def create_supplier_order_response(
        self,
        service_name: str,
        link: str,
        quantity: int,
        *,
        retry_total: int = RETRY_TOTAL,
    ) -> httpx.Response:
        payload = {"url": link, "service_name": service_name, "total_count": quantity}
        logger.info(f"[SUPPLIER] add service={service_name} link={link} qty={quantity}")
        response: httpx.Response = await self.request(
            "POST",
            "/smm-panel/orders",
            json_data=payload,
            response_type="response",
            retry_total=retry_total,
        )
        return response

    async def get_supplier_status(self, order_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self.request("GET", f"/smm-panel/orders/{order_id}")
        return result


smm_panel = SmmPanelApi()
