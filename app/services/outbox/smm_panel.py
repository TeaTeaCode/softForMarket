from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.suppliers.smm_panel import SmmPanelApi
from app.db import outbox_repository as outbox_repo
from app.db.outbox_repository import DueOutboxMessage
from app.services import background
from app.services.outbox.delivery import NonRetryableDeliveryError, OutboxDeliveryResult

CREATE_ORDER_ACTION = "create-supplier-order"
SMM_PANEL_SUPPLIER = "smm_panel"


class SmmPanelCreateOrderPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purchase_id: int = Field(gt=0, strict=True)
    url: str = Field(min_length=1)
    service_name: str = Field(min_length=1)
    total_count: int = Field(gt=0, strict=True)

    def supplier_payload(self) -> dict[str, Any]:
        return self.model_dump(exclude={"purchase_id"})


class SmmPanelCreateOrderReceipt(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = Field(gt=0, strict=True)


class SmmPanelPurchaseReference(BaseModel):
    model_config = ConfigDict(extra="ignore")

    purchase_id: int = Field(gt=0, strict=True)


class SmmPanelOutboxDeliverer:
    def __init__(self, client: SmmPanelApi | None = None) -> None:
        self._client = client or SmmPanelApi(transport_retry_total=0)

    async def deliver(self, event: DueOutboxMessage) -> OutboxDeliveryResult:
        self._validate_route(event)
        payload = self._parse_payload(event.payload)
        response = await self._client.create_supplier_order_response(
            payload.service_name,
            payload.url,
            payload.total_count,
            retry_total=0,
        )
        try:
            supplier_response = response.json()
            receipt = SmmPanelCreateOrderReceipt.model_validate(supplier_response)
        except (ValidationError, ValueError) as error:
            raise NonRetryableDeliveryError(f"SMM Panel returned an invalid successful response: {error}") from error
        return OutboxDeliveryResult(
            response=response,
            supplier_order_id=str(receipt.id),
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
            raise RuntimeError("Successful SMM Panel delivery has no supplier order id")
        return await outbox_repo.complete_smm_panel_delivery(
            session,
            event.id,
            event.lease_token,
            payload.purchase_id,
            result.supplier_order_id,
        )

    async def fail(self, session: AsyncSession, event: DueOutboxMessage, error: str) -> bool:
        reference = self._parse_purchase_reference(event.payload)
        return await outbox_repo.complete_smm_panel_failure(
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
            SMM_PANEL_SUPPLIER,
        )

    async def close(self) -> None:
        await self._client.close()

    @staticmethod
    def _validate_route(event: DueOutboxMessage) -> None:
        if event.supplier != SMM_PANEL_SUPPLIER or event.action != CREATE_ORDER_ACTION:
            raise NonRetryableDeliveryError(f"Unsupported outbox route supplier={event.supplier!r} action={event.action!r}")

    @staticmethod
    def _parse_payload(payload: dict[str, Any]) -> SmmPanelCreateOrderPayload:
        try:
            return SmmPanelCreateOrderPayload.model_validate(payload)
        except ValidationError as error:
            raise NonRetryableDeliveryError(f"Invalid SMM Panel outbox payload: {error}") from error

    @staticmethod
    def _parse_purchase_reference(payload: dict[str, Any]) -> SmmPanelPurchaseReference | None:
        try:
            return SmmPanelPurchaseReference.model_validate(payload)
        except ValidationError:
            return None
