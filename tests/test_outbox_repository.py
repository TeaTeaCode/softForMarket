from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config.settings import settings
from app.db import outbox_repository as outbox_repo
from app.db.models import Base, OutboxMessage, OutboxStatus


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {settings.POSTGRES_SCHEMA}")
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def enqueue(sessions, order_key="CODE-1"):
    async with sessions.begin() as session:
        return await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key=order_key,
            action="create-supplier-order",
            supplier="smm_panel",
            payload={"service_name": "G_BOOST_90", "url": "https://t.me/example", "total_count": 1},
        )


async def get_message(sessions, event_id):
    async with sessions() as session:
        return await session.get(OutboxMessage, event_id)


async def claim_one(sessions):
    async with sessions.begin() as session:
        events = await outbox_repo.claim_due(session, limit=1, lease_seconds=60)
    assert len(events) == 1
    return events[0]


async def test_enqueue_is_idempotent(sessions):
    first_id = await enqueue(sessions)
    duplicate_id = await enqueue(sessions)

    assert first_id is not None
    assert duplicate_id is None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 1


async def test_enqueue_participates_in_callers_transaction(sessions):
    async with sessions() as session:
        await session.begin()
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="ROLLBACK",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={"service_name": "G_BOOST_90"},
        )
        assert event_id is not None
        await session.rollback()

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 0


async def test_claim_due_leases_only_ready_pending_messages(sessions):
    first_id = await enqueue(sessions, "CODE-1")
    second_id = await enqueue(sessions, "CODE-2")
    future_id = await enqueue(sessions, "CODE-FUTURE")
    assert first_id is not None and second_id is not None and future_id is not None

    async with sessions.begin() as session:
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == future_id)
            .values(next_attempt_at=datetime.now(UTC) + timedelta(hours=1))
        )

    async with sessions.begin() as session:
        first_batch = await outbox_repo.claim_due(session, limit=1, lease_seconds=60)
    async with sessions.begin() as session:
        second_batch = await outbox_repo.claim_due(session, limit=10, lease_seconds=60)

    assert len(first_batch) == 1
    assert {event.id for event in first_batch + second_batch} == {first_id, second_id}
    assert all(event.supplier == "smm_panel" for event in first_batch + second_batch)


async def test_mark_delivered_clears_error_and_increments_attempts(sessions):
    event_id = await enqueue(sessions)
    assert event_id is not None
    event = await claim_one(sessions)
    async with sessions.begin() as session:
        await session.execute(
            update(OutboxMessage).where(OutboxMessage.id == event_id).values(attempts=2, last_error="timeout")
        )
        updated = await outbox_repo.mark_delivered(session, event_id, event.lease_token)

    assert updated is True
    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.DELIVERED
    assert message.attempts == 3
    assert message.last_error is None
    assert message.delivered_at is not None


async def test_reschedule_keeps_pending_and_records_error(sessions):
    event_id = await enqueue(sessions)
    assert event_id is not None
    event = await claim_one(sessions)
    before = datetime.now(UTC)
    async with sessions.begin() as session:
        updated = await outbox_repo.reschedule(session, event_id, event.lease_token, "network down", delay_seconds=30)

    assert updated is True
    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.PENDING
    assert message.attempts == 1
    assert message.last_error == "network down"
    next_attempt_at = (
        message.next_attempt_at.replace(tzinfo=UTC) if message.next_attempt_at.tzinfo is None else message.next_attempt_at
    )
    assert next_attempt_at >= before + timedelta(seconds=29)


async def test_mark_dead_truncates_error(sessions):
    event_id = await enqueue(sessions)
    assert event_id is not None
    event = await claim_one(sessions)
    async with sessions.begin() as session:
        updated = await outbox_repo.mark_dead(session, event_id, event.lease_token, "x" * 3000)

    assert updated is True
    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.DEAD
    assert message.attempts == 1
    assert message.last_error == "x" * 2000


async def test_delete_delivered_before_keeps_pending_and_recent(sessions):
    old_id = await enqueue(sessions, "OLD")
    recent_id = await enqueue(sessions, "RECENT")
    pending_id = await enqueue(sessions, "PENDING")
    dead_id = await enqueue(sessions, "DEAD")
    assert old_id is not None and recent_id is not None and pending_id is not None and dead_id is not None

    async with sessions.begin() as session:
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == old_id)
            .values(status=OutboxStatus.DELIVERED, delivered_at=datetime.now(UTC) - timedelta(days=10))
        )
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == recent_id)
            .values(status=OutboxStatus.DELIVERED, delivered_at=datetime.now(UTC))
        )
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == dead_id)
            .values(status=OutboxStatus.DEAD, delivered_at=datetime.now(UTC) - timedelta(days=10))
        )
        deleted = await outbox_repo.delete_delivered_before(session, retention_days=3)

    assert deleted == 1
    assert await get_message(sessions, old_id) is None
    assert await get_message(sessions, recent_id) is not None
    assert await get_message(sessions, pending_id) is not None
    assert await get_message(sessions, dead_id) is not None


async def test_stale_lease_cannot_overwrite_newer_result(sessions):
    event_id = await enqueue(sessions)
    assert event_id is not None
    stale_event = await claim_one(sessions)

    async with sessions.begin() as session:
        await session.execute(
            update(OutboxMessage)
            .where(OutboxMessage.id == event_id)
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    current_event = await claim_one(sessions)

    async with sessions.begin() as session:
        stale_updated = await outbox_repo.mark_delivered(session, event_id, stale_event.lease_token)
        current_updated = await outbox_repo.mark_dead(session, event_id, current_event.lease_token, "rejected")

    assert stale_updated is False
    assert current_updated is True
    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.DEAD
