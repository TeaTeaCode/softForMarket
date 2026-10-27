from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import outbox_repository as outbox_repo
from app.db.models import Purchase
from app.services.outbox.delivery import CREATE_ORDER_ACTION
from app.services.outbox.smm_panel import SMM_PANEL_SUPPLIER, SmmPanelCreateOrderPayload
from app.services.outbox.teateagram import TEATEAGRAM_SUPPLIER, TeaTeaGramCreateOrderPayload


@dataclass(frozen=True, slots=True)
class QueuedPurchase:
    purchase_id: int
    outbox_id: int


def _order_payload(supplier: str, purchase_id: int, service: int | str, url: str, quantity: int) -> dict[str, Any]:
    if supplier == TEATEAGRAM_SUPPLIER:
        fields = {"purchase_id": purchase_id, "service": service, "link": url, "quantity": quantity}
        return TeaTeaGramCreateOrderPayload.model_validate(fields).model_dump()
    if supplier == SMM_PANEL_SUPPLIER:
        fields = {"purchase_id": purchase_id, "url": url, "service_name": service, "total_count": quantity}
        return SmmPanelCreateOrderPayload.model_validate(fields).model_dump()
    raise ValueError(f"Неизвестный поставщик Outbox: {supplier!r}")


async def queue_supplier_purchase(
    session: AsyncSession,
    *,
    purchase_values: dict[str, Any],
    source: str,
    order_key: str,
    supplier: str,
    service: int | str,
    url: str,
    quantity: int,
) -> QueuedPurchase | None:
    """Create a purchase and its delivery message in one owned transaction."""
    async with session.begin():
        return await enqueue_supplier_purchase(
            session,
            purchase_values=purchase_values,
            source=source,
            order_key=order_key,
            supplier=supplier,
            service=service,
            url=url,
            quantity=quantity,
        )


async def enqueue_supplier_purchase(
    session: AsyncSession,
    *,
    purchase_values: dict[str, Any],
    source: str,
    order_key: str,
    supplier: str,
    service: int | str,
    url: str,
    quantity: int,
) -> QueuedPurchase | None:
    """Enqueue inside the caller's active transaction without committing it."""
    stored_purchase_values = {
        **purchase_values,
        "supplier": supplier,
        "supplier_order_id": None,
        "status": "OUTBOX_PENDING",
    }
    purchase = Purchase(**stored_purchase_values)
    session.add(purchase)
    await session.flush()
    outbox_id = await outbox_repo.enqueue(
        session,
        source=source,
        order_key=order_key,
        action=CREATE_ORDER_ACTION,
        supplier=supplier,
        payload=_order_payload(supplier, purchase.id, service, url, quantity),
    )
    if outbox_id is None:
        await session.delete(purchase)
        await session.flush()
        return None
    return QueuedPurchase(purchase_id=purchase.id, outbox_id=outbox_id)
