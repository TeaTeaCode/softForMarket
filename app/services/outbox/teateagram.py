from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.suppliers.teateagram import TeaTeaGramApi
from app.db import outbox_repository as outbox_repo
from app.db.outbox_repository import DueOutboxMessage
from app.services import background
from app.services.outbox.delivery import CREATE_ORDER_ACTION, NonRetryableDeliveryError, OutboxDeliveryResult

TEATEAGRAM_SUPPLIER = "teateagram"


class TeaTeaGramCreateOrderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purchase_id: int = Field(gt=0, strict=True)
    service: int = Field(gt=0, strict=True)
    link: str = Field(min_length=1)
    quantity: int = Field(gt=0, strict=True)


class TeaTeaGramCreateOrderReceipt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # id может прийти строкой
    order: int = Field(gt=0)


class TeaTeaGramPurchaseReference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    purchase_id: int = Field(gt=0, strict=True)


class TeaTeaGramOutboxDeliverer:
    def __init__(self, client: TeaTeaGramApi | None = None) -> None:
        self._client = client or TeaTeaGramApi(transport_retry_total=0)

    async def deliver(self, event: DueOutboxMessage) -> OutboxDeliveryResult:
        self._validate_route(event)
        payload = self._parse_payload(event.payload)
        response = await self._client.create_supplier_order_response(
            payload.service,
            payload.link,
            payload.quantity,
            retry_total=0,
        )
        try:
            supplier_response = response.json()
            receipt = TeaTeaGramCreateOrderReceipt.model_validate(supplier_response)
        except (ValidationError, ValueError) as error:
            # отказ приходит с кодом 200: {"error": "..."}
            raise NonRetryableDeliveryError(f"socpanel не принял заказ: {response.text[:500]}") from error
        return OutboxDeliveryResult(
            response=response,
            supplier_order_id=str(receipt.order),
            supplier_response=supplier_response,
        )

    async def complete(
        self,
        session: AsyncSession,
        event: DueOutboxMessage,
        result: OutboxDeliveryResult | None,
    ) -> bool:
        payload = self._parse_payload(event.payload)
        if result is None or result.supplier_order_id is None:
            raise RuntimeError("Successful socpanel delivery has no supplier order id")
        return await outbox_repo.complete_supplier_delivery(
            session,
            event.id,
            event.lease_token,
            payload.purchase_id,
            TEATEAGRAM_SUPPLIER,
            result.supplier_order_id,
        )

    async def fail(self, session: AsyncSession, event: DueOutboxMessage, error: str) -> bool:
        reference = self._parse_purchase_reference(event.payload)
        return await outbox_repo.complete_supplier_failure(
            session,
            event.id,
            event.lease_token,
            reference.purchase_id if reference is not None else None,
            error,
        )

    async def after_complete(self, event: DueOutboxMessage, result: OutboxDeliveryResult | None) -> None:
        if result is None or result.supplier_order_id is None or result.supplier_response is None:
            return
        background.schedule_outbox_status_check(
            event.order_key,
            result.supplier_order_id,
            result.supplier_response,
            TEATEAGRAM_SUPPLIER,
        )

    async def close(self) -> None:
        await self._client.close()

    @staticmethod
    def _validate_route(event: DueOutboxMessage) -> None:
        if event.supplier != TEATEAGRAM_SUPPLIER or event.action != CREATE_ORDER_ACTION:
            raise NonRetryableDeliveryError(f"Unsupported outbox route supplier={event.supplier!r} action={event.action!r}")

    @staticmethod
    def _parse_payload(payload: dict[str, Any]) -> TeaTeaGramCreateOrderPayload:
        try:
            return TeaTeaGramCreateOrderPayload.model_validate(payload)
        except ValidationError as error:
            raise NonRetryableDeliveryError(f"Invalid socpanel outbox payload: {error}") from error

    @staticmethod
    def _parse_purchase_reference(payload: dict[str, Any]) -> TeaTeaGramPurchaseReference | None:
        try:
            return TeaTeaGramPurchaseReference.model_validate(payload)
        except ValidationError:
            return None
