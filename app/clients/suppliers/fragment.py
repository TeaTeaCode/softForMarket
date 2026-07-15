from typing import Any

from loguru import logger

from app.clients.base import BaseApi
from app.core.config.settings import settings

# Терминальные статусы задачи: дальше опрашивать нет смысла
FINAL_STATUSES = frozenset({"success", "failed"})

# Fragment принимает только эти сроки подписки, иначе 422
PREMIUM_MONTHS = frozenset({3, 6, 12})


class FragmentApi(BaseApi):
    def __init__(self) -> None:
        super().__init__(
            base_url=settings.FRAGMENT_BASE_URL.rstrip("/"),
            headers={"X-API-Key": settings.FRAGMENT_API_KEY.get_secret_value()},
        )

    async def create_stars_order(self, username: str, quantity: int) -> str:
        """Покупка Stars. Возвращает task_id — покупка асинхронная."""
        payload = {"username": username, "quantity": quantity}
        logger.info(f"[FRAGMENT] stars username={username} qty={quantity}")
        data: dict[str, Any] = await self.request("POST", "/api/purchase/stars", json_data=payload)
        logger.info(f"[FRAGMENT] ответ на stars: {data}")
        return self._task_id(data)

    async def create_premium_order(self, username: str, months: int) -> str:
        """Покупка Premium. Возвращает task_id — покупка асинхронная."""
        if months not in PREMIUM_MONTHS:
            raise RuntimeError(f"Fragment принимает подписку только на {sorted(PREMIUM_MONTHS)} мес., получено: {months}")
        payload = {"username": username, "months": months}
        logger.info(f"[FRAGMENT] premium username={username} months={months}")
        data: dict[str, Any] = await self.request("POST", "/api/purchase/premium", json_data=payload)
        logger.info(f"[FRAGMENT] ответ на premium: {data}")
        return self._task_id(data)

    async def get_task_status(self, task_id: str) -> dict[str, Any]:
        """Статус задачи: pending | processing | awaiting_balance | success | failed."""
        result: dict[str, Any] = await self.request("GET", f"/api/tasks/{task_id}")
        return result

    async def check_username(self, username: str) -> bool:
        """True — получатель существует и может принять покупку."""
        result = await self.request("POST", "/api/check-username", json_data={"username": username})
        return bool(result)

    @staticmethod
    def _task_id(data: dict[str, Any]) -> str:
        task_id = str(data.get("task_id") or "").strip()
        if not task_id:
            raise RuntimeError(f"Fragment не вернул task_id: {data}")
        return task_id


fragment = FragmentApi()
