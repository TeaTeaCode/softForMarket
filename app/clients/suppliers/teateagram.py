from typing import Any

from app.clients.base import BaseApi
from app.core.config.settings import settings

TEA_API_BASE = "https://teateagram.com/api/v2"


class TeaTeaGramApi(BaseApi):
    async def get_supplier_status(self, order_id: str) -> dict[str, Any]:
        payload = {"key": settings.TEA_API_KEY.get_secret_value(), "action": "status", "order": order_id}
        result: dict[str, Any] = await self.request("POST", TEA_API_BASE, data=payload)
        return result


teateagram = TeaTeaGramApi()
