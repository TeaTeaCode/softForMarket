import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config.settings import settings
from app.db import outbox_repository as outbox_repo
from app.db.models import Base, OutboxMessage, OutboxStatus
from app.services.outbox.delivery import OutboxDeliveryResult
from app.services.outbox.worker import OutboxWorker, OutboxWorkerConfig


class FakeDeliverer:
    def __init__(self, error=None, before_delivery=None, status_code=200, after_error=None):
        self.error = error
        self.before_delivery = before_delivery
        self.status_code = status_code
        self.after_error = after_error
        self.events = []
        self.completed_events = []

    async def deliver(self, event):
        self.events.append(event)
        if self.before_delivery is not None:
            await self.before_delivery(event)
        if self.error is not None:
            raise self.error
        request = httpx.Request("POST", "https://supplier.example/orders")
        return OutboxDeliveryResult(httpx.Response(self.status_code, request=request))

    async def complete(self, session, event, result):
        return await outbox_repo.mark_delivered(session, event.id, event.lease_token)

    async def fail(self, session, event, error):
        return await outbox_repo.mark_dead(session, event.id, event.lease_token, error)

    async def after_complete(self, event, result):
        self.completed_events.append(event)
        if self.after_error is not None:
            raise self.after_error


class ConcurrentDeliverer:
    def __init__(self):
        self.active = 0
        self.max_active = 0

    async def deliver(self, event):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        request = httpx.Request("POST", "https://supplier.example/orders")
        return OutboxDeliveryResult(httpx.Response(200, request=request))

    async def complete(self, session, event, result):
        return await outbox_repo.mark_delivered(session, event.id, event.lease_token)

    async def fail(self, session, event, error):
        return await outbox_repo.mark_dead(session, event.id, event.lease_token, error)

    async def after_complete(self, event, result):
        return None


class BlockingDeliverer:
    def __init__(self):
        self.started = asyncio.Event()

    async def deliver(self, event):
        self.started.set()
        await asyncio.Event().wait()

    async def complete(self, session, event, result):
        return await outbox_repo.mark_delivered(session, event.id, event.lease_token)

    async def fail(self, session, event, error):
        return await outbox_repo.mark_dead(session, event.id, event.lease_token, error)

    async def after_complete(self, event, result):
        return None


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {settings.POSTGRES_SCHEMA}")
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest.fixture
def worker_config():
    return OutboxWorkerConfig(
        poll_interval=0.01,
        batch_size=10,
        max_concurrency=10,
        lease_seconds=60,
        shutdown_grace=1,
        backoff_base=2,
        backoff_max=300,
        cleanup_interval=3600,
        cleanup_retention_days=3,
    )


async def enqueue(sessions, order_key="CODE-1"):
    async with sessions.begin() as session:
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key=order_key,
            action="create-supplier-order",
            supplier="smm_panel",
            payload={"service_name": "G_BOOST_90", "url": "https://t.me/example", "total_count": 1},
        )
    assert event_id is not None
    return event_id


async def get_message(sessions, event_id):
    async with sessions() as session:
        return await session.get(OutboxMessage, event_id)


async def test_successful_delivery_marks_message_delivered(sessions, worker_config):
    event_id = await enqueue(sessions)
    deliverer = FakeDeliverer()
    worker = OutboxWorker(worker_config, sessions, deliverer)

    assert await worker.tick() == 1

    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.DELIVERED
    assert message.attempts == 1
    assert message.delivered_at is not None
    assert len(deliverer.events) == 1
    assert len(deliverer.completed_events) == 1


async def test_after_complete_failure_does_not_undo_delivered_message(sessions, worker_config):
    event_id = await enqueue(sessions)
    deliverer = FakeDeliverer(after_error=RuntimeError("notification failed"))
    worker = OutboxWorker(worker_config, sessions, deliverer)

    assert await worker.tick() == 1

    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.DELIVERED
    assert message.attempts == 1


async def test_claim_is_committed_before_network_delivery(sessions, worker_config):
    event_id = await enqueue(sessions)

    async def assert_claim_visible(event):
        async with sessions() as session:
            message = await session.get(OutboxMessage, event.id)
            assert message is not None
            assert message.lease_token == event.lease_token
            assert message.next_attempt_at > datetime.now(UTC).replace(tzinfo=None)

    worker = OutboxWorker(worker_config, sessions, FakeDeliverer(before_delivery=assert_claim_visible))

    assert await worker.tick() == 1
    assert (await get_message(sessions, event_id)).status == OutboxStatus.DELIVERED


async def test_claimed_batch_is_delivered_concurrently(sessions, worker_config):
    await enqueue(sessions, "CODE-1")
    await enqueue(sessions, "CODE-2")
    deliverer = ConcurrentDeliverer()
    worker = OutboxWorker(worker_config, sessions, deliverer)

    assert await worker.tick() == 2
    assert deliverer.max_active == 2


async def test_tick_claims_no_more_than_max_concurrency(sessions, worker_config):
    await enqueue(sessions, "CODE-1")
    await enqueue(sessions, "CODE-2")
    deliverer = FakeDeliverer()
    worker = OutboxWorker(replace(worker_config, batch_size=10, max_concurrency=1), sessions, deliverer)

    assert await worker.tick() == 1
    assert len(deliverer.events) == 1


async def test_network_error_reschedules_message(sessions, worker_config):
    event_id = await enqueue(sessions)
    worker = OutboxWorker(worker_config, sessions, FakeDeliverer(error=httpx.ConnectError("network down")))
    worker.next_delay = lambda attempts: 30

    assert await worker.tick() == 1

    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.PENDING
    assert message.attempts == 1
    assert message.lease_token is None
    assert message.last_error == "ConnectError: network down"


async def test_returned_5xx_response_is_rescheduled(sessions, worker_config):
    event_id = await enqueue(sessions)
    worker = OutboxWorker(worker_config, sessions, FakeDeliverer(status_code=503))
    worker.next_delay = lambda attempts: 30

    assert await worker.tick() == 1
    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.PENDING
    assert message.attempts == 1


async def test_final_client_error_marks_message_dead(sessions, worker_config):
    event_id = await enqueue(sessions)
    request = httpx.Request("POST", "https://supplier.example/orders")
    response = httpx.Response(422, request=request, json={"detail": "invalid payload"})
    error = httpx.HTTPStatusError("unprocessable", request=request, response=response)
    worker = OutboxWorker(worker_config, sessions, FakeDeliverer(error=error))

    assert await worker.tick() == 1

    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.DEAD
    assert message.attempts == 1
    assert "HTTPStatusError" in str(message.last_error)


async def test_409_marks_message_dead(sessions, worker_config):
    event_id = await enqueue(sessions)
    request = httpx.Request("POST", "https://supplier.example/orders")
    response = httpx.Response(409, request=request, json={"detail": "already processed"})
    error = httpx.HTTPStatusError("conflict", request=request, response=response)
    worker = OutboxWorker(worker_config, sessions, FakeDeliverer(error=error))

    assert await worker.tick() == 1
    assert (await get_message(sessions, event_id)).status == OutboxStatus.DEAD


def test_worker_config_uses_application_settings():
    config = OutboxWorkerConfig.from_settings(settings)

    assert config.poll_interval == settings.OUTBOX_POLL_INTERVAL
    assert config.batch_size == settings.OUTBOX_BATCH_SIZE
    assert config.max_concurrency == settings.OUTBOX_MAX_CONCURRENCY
    assert config.lease_seconds == settings.OUTBOX_LEASE_SECONDS
    assert config.shutdown_grace == settings.OUTBOX_SHUTDOWN_GRACE_SECONDS
    assert config.backoff_base == settings.OUTBOX_BACKOFF_BASE
    assert config.backoff_max == settings.OUTBOX_BACKOFF_MAX
    assert config.cleanup_interval == settings.OUTBOX_CLEANUP_INTERVAL
    assert config.cleanup_retention_days == settings.OUTBOX_CLEANUP_RETENTION_DAYS


async def test_backoff_is_capped_for_unlimited_retries(worker_config, sessions):
    worker = OutboxWorker(worker_config, sessions, FakeDeliverer())

    assert 0 <= worker.next_delay(10_000) <= worker_config.backoff_max


@pytest.mark.parametrize(
    "overrides",
    [
        {"batch_size": 0},
        {"max_concurrency": 0},
        {"lease_seconds": 0},
        {"shutdown_grace": 0},
        {"backoff_base": 301},
    ],
)
def test_worker_config_rejects_values_that_break_delivery(worker_config, overrides):
    with pytest.raises(ValueError):
        replace(worker_config, **overrides)


async def test_stop_cancels_delivery_after_grace_timeout_without_recording_outcome(sessions, worker_config):
    event_id = await enqueue(sessions)
    deliverer = BlockingDeliverer()
    worker = OutboxWorker(replace(worker_config, shutdown_grace=0.01), sessions, deliverer)

    worker.start()
    await asyncio.wait_for(deliverer.started.wait(), timeout=1)
    await worker.stop()

    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.PENDING
    assert message.attempts == 0
    assert message.lease_token is not None


async def test_stop_drains_active_delivery_before_exit(sessions, worker_config):
    event_id = await enqueue(sessions)
    release = asyncio.Event()
    started = asyncio.Event()

    async def wait_for_release(event):
        started.set()
        await release.wait()

    deliverer = FakeDeliverer(before_delivery=wait_for_release)
    worker = OutboxWorker(worker_config, sessions, deliverer)

    worker.start()
    await asyncio.wait_for(started.wait(), timeout=1)
    stop_task = asyncio.create_task(worker.stop())
    await asyncio.sleep(0)
    assert not stop_task.done()
    release.set()
    await stop_task

    message = await get_message(sessions, event_id)
    assert message is not None
    assert message.status == OutboxStatus.DELIVERED
