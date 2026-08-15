from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db import outbox_repository as outbox_repo
from app.db.models import Purchase
from app.services.outbox.smm_panel import (
    CREATE_ORDER_ACTION,
    SMM_PANEL_SUPPLIER,
    SmmPanelCreateOrderPayload,
)


@dataclass(frozen=True, slots=True)
class QueuedSmmPanelPurchase:
    purchase_id: int
    outbox_id: int


async def queue_smm_panel_purchase(
    session: AsyncSession,
    *,
    purchase_values: dict[str, Any],
    source: str,
    order_key: str,
    service_name: str,
    url: str,
    total_count: int,
) -> QueuedSmmPanelPurchase | None:
    """Create a purchase and its delivery message in one owned transaction."""
    async with session.begin():
        return await enqueue_smm_panel_purchase(
            session,
            purchase_values=purchase_values,
            source=source,
            order_key=order_key,
            service_name=service_name,
            url=url,
            total_count=total_count,
        )


async def enqueue_smm_panel_purchase(
    session: AsyncSession,
    *,
    purchase_values: dict[str, Any],
    source: str,
    order_key: str,
    service_name: str,
    url: str,
    total_count: int,
) -> QueuedSmmPanelPurchase | None:
    """Enqueue inside the caller's active transaction without committing it."""
    stored_purchase_values = {
        **purchase_values,
        "supplier": SMM_PANEL_SUPPLIER,
        "supplier_order_id": None,
        "status": "OUTBOX_PENDING",
    }
    purchase = Purchase(**stored_purchase_values)
    session.add(purchase)
    await session.flush()
    payload = SmmPanelCreateOrderPayload(
        purchase_id=purchase.id,
        url=url,
        service_name=service_name,
        total_count=total_count,
    )
    outbox_id = await outbox_repo.enqueue(
        session,
        source=source,
        order_key=order_key,
        action=CREATE_ORDER_ACTION,
        supplier=SMM_PANEL_SUPPLIER,
        payload=payload.model_dump(),
    )
    if outbox_id is None:
        await session.delete(purchase)
        await session.flush()
        return None
    return QueuedSmmPanelPurchase(purchase_id=purchase.id, outbox_id=outbox_id)
