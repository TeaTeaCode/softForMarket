import asyncio
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import outbox_repository as outbox_repo
from app.db.outbox_repository import DueOutboxMessage

CREATE_ORDER_ACTION = "create-supplier-order"


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

    async def close(self) -> None: ...


class OutboxRouter:
    """Отдаёт сообщение обработчику его поставщика."""

    def __init__(self, deliverers: dict[str, OutboxDeliverer]) -> None:
        self._deliverers = deliverers

    def _route(self, event: DueOutboxMessage) -> OutboxDeliverer:
        deliverer = self._deliverers.get(event.supplier)
        if deliverer is None:
            raise NonRetryableDeliveryError(f"Unsupported outbox route supplier={event.supplier!r} action={event.action!r}")
        return deliverer

    async def deliver(self, event: DueOutboxMessage) -> OutboxDeliveryResult:
        return await self._route(event).deliver(event)

    async def complete(
        self,
        session: AsyncSession,
        event: DueOutboxMessage,
        result: OutboxDeliveryResult | None,
    ) -> bool:
        return await self._route(event).complete(session, event, result)

    async def fail(self, session: AsyncSession, event: DueOutboxMessage, error: str) -> bool:
        deliverer = self._deliverers.get(event.supplier)
        if deliverer is None:
            return await outbox_repo.mark_dead(session, event.id, event.lease_token, error)
        return await deliverer.fail(session, event, error)

    async def after_complete(self, event: DueOutboxMessage, result: OutboxDeliveryResult | None) -> None:
        await self._route(event).after_complete(event, result)

    async def close(self) -> None:
        await asyncio.gather(*(deliverer.close() for deliverer in self._deliverers.values()))


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
