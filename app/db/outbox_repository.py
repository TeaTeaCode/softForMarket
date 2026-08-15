"""Outbox operations run inside transactions owned by their callers."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import OutboxMessage, OutboxStatus, Purchase

ERROR_TEXT_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class DueOutboxMessage:
    id: int
    source: str
    order_key: str
    action: str
    supplier: str
    payload: dict[str, Any]
    attempts: int
    lease_token: str

    @property
    def delivery_id(self) -> str:
        return f"{self.source}:{self.order_key}:{self.action}"


async def enqueue(
    session: AsyncSession,
    *,
    source: str,
    order_key: str,
    action: str,
    supplier: str,
    payload: dict[str, Any],
) -> int | None:
    """Create one pending message per logical delivery; return its id or None for a duplicate."""
    statement = (
        pg_insert(OutboxMessage)
        .values(
            source=source,
            order_key=order_key,
            action=action,
            supplier=supplier,
            payload=payload,
            status=OutboxStatus.PENDING,
            attempts=0,
        )
        .on_conflict_do_nothing(index_elements=["source", "order_key", "action"])
        .returning(OutboxMessage.id)
    )
    return (await session.execute(statement)).scalar_one_or_none()


async def claim_due(session: AsyncSession, limit: int, lease_seconds: int) -> list[DueOutboxMessage]:
    """Lease ready messages without holding database locks during network delivery."""
    lease_until = await _database_now(session) + timedelta(seconds=lease_seconds)
    lease_token = str(uuid4())
    due_ids = (
        select(OutboxMessage.id)
        .where(OutboxMessage.status == OutboxStatus.PENDING, OutboxMessage.next_attempt_at <= func.now())
        .order_by(OutboxMessage.next_attempt_at, OutboxMessage.id)
        .with_for_update(skip_locked=True)
        .limit(limit)
        .scalar_subquery()
    )
    statement = (
        update(OutboxMessage)
        .where(OutboxMessage.id.in_(due_ids))
        .values(next_attempt_at=lease_until, lease_token=lease_token)
        .returning(
            OutboxMessage.id,
            OutboxMessage.source,
            OutboxMessage.order_key,
            OutboxMessage.action,
            OutboxMessage.supplier,
            OutboxMessage.payload,
            OutboxMessage.attempts,
            OutboxMessage.lease_token,
        )
    )
    rows = (await session.execute(statement)).all()
    return [
        DueOutboxMessage(
            id=row.id,
            source=row.source,
            order_key=row.order_key,
            action=row.action,
            supplier=row.supplier,
            payload=row.payload,
            attempts=row.attempts,
            lease_token=row.lease_token,
        )
        for row in rows
    ]


async def mark_delivered(session: AsyncSession, event_id: int, lease_token: str) -> bool:
    statement = (
        update(OutboxMessage)
        .where(
            OutboxMessage.id == event_id,
            OutboxMessage.status == OutboxStatus.PENDING,
            OutboxMessage.lease_token == lease_token,
        )
        .values(
            status=OutboxStatus.DELIVERED,
            delivered_at=func.now(),
            last_error=None,
            lease_token=None,
            attempts=OutboxMessage.attempts + 1,
        )
        .returning(OutboxMessage.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def complete_smm_panel_delivery(
    session: AsyncSession,
    event_id: int,
    lease_token: str,
    purchase_id: int,
    supplier_order_id: str,
) -> bool:
    if not await mark_delivered(session, event_id, lease_token):
        return False
    values = {
        "supplier": "smm_panel",
        "supplier_order_id": supplier_order_id,
        "status": "SUPPLIER_ACCEPTED",
    }
    purchase = await session.scalar(update(Purchase).where(Purchase.id == purchase_id).values(**values).returning(Purchase.id))
    if purchase is None:
        raise RuntimeError(f"Purchase {purchase_id} referenced by outbox message {event_id} does not exist")
    return True


async def complete_smm_panel_failure(
    session: AsyncSession,
    event_id: int,
    lease_token: str,
    purchase_id: int | None,
    error: str,
) -> bool:
    if not await mark_dead(session, event_id, lease_token, error):
        return False
    if purchase_id is not None:
        purchase = await session.scalar(
            update(Purchase)
            .where(Purchase.id == purchase_id)
            .values(status="OUTBOX_DELIVERY_ERROR", supplier_status=error[:ERROR_TEXT_LIMIT])
            .returning(Purchase.id)
        )
        if purchase is None:
            raise RuntimeError(f"Purchase {purchase_id} referenced by outbox message {event_id} does not exist")
    return True


async def reschedule(session: AsyncSession, event_id: int, lease_token: str, error: str, delay_seconds: float) -> bool:
    next_attempt_at = await _database_now(session) + timedelta(seconds=delay_seconds)
    statement = (
        update(OutboxMessage)
        .where(
            OutboxMessage.id == event_id,
            OutboxMessage.status == OutboxStatus.PENDING,
            OutboxMessage.lease_token == lease_token,
        )
        .values(
            attempts=OutboxMessage.attempts + 1,
            last_error=error[:ERROR_TEXT_LIMIT],
            lease_token=None,
            next_attempt_at=next_attempt_at,
        )
        .returning(OutboxMessage.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def mark_dead(session: AsyncSession, event_id: int, lease_token: str, error: str) -> bool:
    statement = (
        update(OutboxMessage)
        .where(
            OutboxMessage.id == event_id,
            OutboxMessage.status == OutboxStatus.PENDING,
            OutboxMessage.lease_token == lease_token,
        )
        .values(
            status=OutboxStatus.DEAD,
            attempts=OutboxMessage.attempts + 1,
            last_error=error[:ERROR_TEXT_LIMIT],
            lease_token=None,
        )
        .returning(OutboxMessage.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


async def delete_delivered_before(session: AsyncSession, retention_days: int) -> int:
    cutoff = await _database_now(session) - timedelta(days=retention_days)
    deleted_ids = await session.scalars(
        delete(OutboxMessage)
        .where(
            OutboxMessage.status == OutboxStatus.DELIVERED,
            OutboxMessage.delivered_at < cutoff,
        )
        .returning(OutboxMessage.id)
    )
    return len(deleted_ids.all())


async def _database_now(session: AsyncSession) -> datetime:
    value = await session.scalar(select(func.now()))
    if not isinstance(value, datetime):
        raise RuntimeError("Database did not return a timestamp for now()")
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
