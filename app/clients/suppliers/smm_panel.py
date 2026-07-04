from typing import Any

from loguru import logger

from app.clients.base import BaseApi
from app.core.config.settings import settings


class SmmPanelApi(BaseApi):
    def __init__(self) -> None:
        super().__init__(
            base_url=f"{settings.SMM_PANEL_BASE_URL.rstrip('/')}/api/v1/external",
            headers={"X-API-Key": settings.SMM_PANEL_API_KEY.get_secret_value()},
        )

    async def create_supplier_order(self, service_name: str, link: str, quantity: int) -> dict[str, Any]:
        payload = {"url": link, "service_name": service_name, "total_count": quantity}
        logger.info(f"[SUPPLIER] add service={service_name} link={link} qty={quantity}")
        data: dict[str, Any] = await self.request("POST", "/smm-panel/orders", json_data=payload)
        logger.info(f"[SUPPLIER] ответ на add: {data}")
        if "id" not in data:
            raise RuntimeError(f"Поставщик не принял заказ: {data}")
        # "order" — для совместимости с кодом, ждущим формат TeaTeaGram
        data["order"] = data["id"]
        return data

    async def get_supplier_status(self, order_id: str) -> dict[str, Any]:
        result: dict[str, Any] = await self.request("GET", f"/smm-panel/orders/{order_id}")
        return result


smm_panel = SmmPanelApi()
