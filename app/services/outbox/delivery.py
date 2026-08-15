from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.outbox_repository import DueOutboxMessage


class DeliveryOutcome(StrEnum):
    DELIVERED = "delivered"
    RETRY = "retry"
    DEAD = "dead"


class NonRetryableDeliveryError(RuntimeError):
    """The local message or a successful supplier response violates the expected contract."""


@dataclass(frozen=True, slots=True)
class OutboxDeliveryResult:
    response: httpx.Response
    supplier_order_id: str | None = None
    supplier_response: dict[str, Any] | None = None


class OutboxDeliverer(Protocol):
    async def deliver(self, event: DueOutboxMessage) -> OutboxDeliveryResult: ...

    async def complete(
        self,
        session: AsyncSession,
        event: DueOutboxMessage,
        result: OutboxDeliveryResult | None,
    ) -> bool: ...

    async def fail(
        self,
        session: AsyncSession,
        event: DueOutboxMessage,
        error: str,
    ) -> bool: ...

    async def after_complete(
        self,
        event: DueOutboxMessage,
        result: OutboxDeliveryResult | None,
    ) -> None: ...


def classify_delivery_error(error: BaseException) -> DeliveryOutcome:
    if isinstance(error, NonRetryableDeliveryError):
        return DeliveryOutcome.DEAD
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        if status_code in {408, 429} or status_code >= 500:
            return DeliveryOutcome.RETRY
        if 400 <= status_code < 500:
            return DeliveryOutcome.DEAD
        return DeliveryOutcome.RETRY
    if isinstance(error, httpx.TransportError | httpx.TimeoutException):
        return DeliveryOutcome.RETRY
    return DeliveryOutcome.RETRY
