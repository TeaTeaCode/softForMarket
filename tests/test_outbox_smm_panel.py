import json
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.suppliers.smm_panel import SmmPanelApi
from app.core.config.settings import settings
from app.db import outbox_repository as outbox_repo
from app.db.models import Base, OutboxMessage, OutboxStatus, Purchase
from app.services.outbox.delivery import DeliveryOutcome, NonRetryableDeliveryError, classify_delivery_error
from app.services.outbox.smm_panel import SmmPanelOutboxDeliverer
from app.services.outbox.worker import OutboxWorker, OutboxWorkerConfig


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {settings.POSTGRES_SCHEMA}")
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


def make_api(handler):
    api = SmmPanelApi()
    api._client = httpx.AsyncClient(
        base_url="https://supplier.test/api/v1/external",
        transport=httpx.MockTransport(handler),
    )
    return api


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


async def test_default_deliverer_disables_hidden_http_transport_retries():
    deliverer = SmmPanelOutboxDeliverer()

    assert deliverer._client._transport_retry_total == 0
    await deliverer.close()


async def test_deliverer_sends_only_supplier_contract_and_extracts_integer_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"id": 417, "status": "pending"})

    deliverer = SmmPanelOutboxDeliverer(make_api(handler))
    event = outbox_repo.DueOutboxMessage(
        id=1,
        source="ggsel",
        order_key="CODE-1",
        action="create-supplier-order",
        supplier="smm_panel",
        payload={
            "purchase_id": 23,
            "url": "https://t.me/example",
            "service_name": "G_BOOST_90",
            "total_count": 2,
        },
        attempts=0,
        lease_token="lease",
    )

    result = await deliverer.deliver(event)

    assert seen == {
        "path": "/api/v1/external/smm-panel/orders",
        "body": {"url": "https://t.me/example", "service_name": "G_BOOST_90", "total_count": 2},
    }
    assert result.supplier_order_id == "417"
    await deliverer.close()


async def test_deliverer_rejects_success_response_without_id_without_retry():
    deliverer = SmmPanelOutboxDeliverer(make_api(lambda request: httpx.Response(200, json={"status": "pending"})))
    event = outbox_repo.DueOutboxMessage(
        id=1,
        source="ggsel",
        order_key="CODE-1",
        action="create-supplier-order",
        supplier="smm_panel",
        payload={
            "purchase_id": 23,
            "url": "https://t.me/example",
            "service_name": "G_BOOST_90",
            "total_count": 2,
        },
        attempts=0,
        lease_token="lease",
    )

    with pytest.raises(NonRetryableDeliveryError) as exc_info:
        await deliverer.deliver(event)

    assert classify_delivery_error(exc_info.value) == DeliveryOutcome.DEAD
    await deliverer.close()


async def test_deliverer_leaves_http_retries_to_outbox_worker():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "temporarily unavailable"})

    deliverer = SmmPanelOutboxDeliverer(make_api(handler))
    event = outbox_repo.DueOutboxMessage(
        id=1,
        source="ggsel",
        order_key="CODE-1",
        action="create-supplier-order",
        supplier="smm_panel",
        payload={
            "purchase_id": 23,
            "url": "https://t.me/example",
            "service_name": "G_BOOST_90",
            "total_count": 2,
        },
        attempts=0,
        lease_token="lease",
    )

    with pytest.raises(httpx.HTTPStatusError):
        await deliverer.deliver(event)

    assert calls == 1
    await deliverer.close()


async def test_worker_persists_smm_order_id_and_outbox_success_atomically(sessions):
    async with sessions.begin() as session:
        purchase = Purchase(platform="ggsel", unique_code="CODE-1", supplier="smm_panel", status="OUTBOX_PENDING")
        session.add(purchase)
        await session.flush()
        purchase_id = purchase.id
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={
                "purchase_id": purchase_id,
                "url": "https://t.me/example",
                "service_name": "G_BOOST_90",
                "total_count": 2,
            },
        )
    assert event_id is not None

    deliverer = SmmPanelOutboxDeliverer(make_api(lambda request: httpx.Response(200, json={"id": 417})))
    worker = OutboxWorker(worker_config(), sessions, deliverer)

    assert await worker.tick() == 1

    async with sessions() as session:
        purchase = await session.get(Purchase, purchase_id)
        message = await session.get(OutboxMessage, event_id)
    assert purchase is not None
    assert purchase.supplier_order_id == "417"
    assert purchase.status == "SUPPLIER_ACCEPTED"
    assert message is not None
    assert message.status == OutboxStatus.DELIVERED
    await deliverer.close()


async def test_success_schedules_existing_one_shot_status_check(sessions):
    async with sessions.begin() as session:
        purchase = Purchase(platform="ggsel", unique_code="CODE-1", supplier="smm_panel", status="OUTBOX_PENDING")
        session.add(purchase)
        await session.flush()
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={
                "purchase_id": purchase.id,
                "url": "https://t.me/example",
                "service_name": "G_BOOST_90",
                "total_count": 2,
            },
        )
    assert event_id is not None

    supplier_response = {"id": 417, "status": "pending"}
    deliverer = SmmPanelOutboxDeliverer(make_api(lambda request: httpx.Response(200, json=supplier_response)))
    worker = OutboxWorker(worker_config(), sessions, deliverer)

    with patch("app.services.outbox.smm_panel.background.schedule_outbox_status_check") as schedule:
        assert await worker.tick() == 1

    schedule.assert_called_once_with(
        "CODE-1",
        "417",
        supplier_response,
        "smm_panel",
    )
    await deliverer.close()


async def test_worker_marks_purchase_delivery_error_on_409_without_retrying(sessions):
    async with sessions.begin() as session:
        purchase = Purchase(platform="ggsel", unique_code="CODE-1", supplier="smm_panel", status="OUTBOX_PENDING")
        session.add(purchase)
        await session.flush()
        purchase_id = purchase.id
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={
                "purchase_id": purchase_id,
                "url": "https://t.me/example",
                "service_name": "G_BOOST_90",
                "total_count": 2,
            },
        )
    assert event_id is not None

    deliverer = SmmPanelOutboxDeliverer(make_api(lambda request: httpx.Response(409, json={"detail": "already accepted"})))
    worker = OutboxWorker(worker_config(), sessions, deliverer)

    assert await worker.tick() == 1

    async with sessions() as session:
        purchase = await session.get(Purchase, purchase_id)
        message = await session.get(OutboxMessage, event_id)
    assert purchase is not None
    assert purchase.supplier_order_id is None
    assert purchase.status == "OUTBOX_DELIVERY_ERROR"
    assert "409" in str(purchase.supplier_status)
    assert message is not None
    assert message.status == OutboxStatus.DEAD
    assert message.attempts == 1
    await deliverer.close()


async def test_final_supplier_error_marks_purchase_and_outbox_failed(sessions):
    async with sessions.begin() as session:
        purchase = Purchase(platform="ggsel", unique_code="CODE-1", supplier="smm_panel", status="OUTBOX_PENDING")
        session.add(purchase)
        await session.flush()
        purchase_id = purchase.id
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={
                "purchase_id": purchase_id,
                "url": "https://t.me/example",
                "service_name": "G_BOOST_90",
                "total_count": 2,
            },
        )
    assert event_id is not None

    deliverer = SmmPanelOutboxDeliverer(make_api(lambda request: httpx.Response(422, json={"detail": "invalid service"})))
    worker = OutboxWorker(worker_config(), sessions, deliverer)

    assert await worker.tick() == 1

    async with sessions() as session:
        purchase = await session.get(Purchase, purchase_id)
        message = await session.get(OutboxMessage, event_id)
    assert purchase is not None
    assert purchase.status == "OUTBOX_DELIVERY_ERROR"
    assert "422" in str(purchase.supplier_status)
    assert "invalid service" in str(purchase.supplier_status)
    assert message is not None
    assert message.status == OutboxStatus.DEAD
    assert "422" in str(message.last_error)
    assert "invalid service" in str(message.last_error)
    await deliverer.close()


async def test_invalid_success_response_marks_purchase_and_outbox_failed(sessions):
    async with sessions.begin() as session:
        purchase = Purchase(platform="ggsel", unique_code="CODE-1", supplier="smm_panel", status="OUTBOX_PENDING")
        session.add(purchase)
        await session.flush()
        purchase_id = purchase.id
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={
                "purchase_id": purchase_id,
                "url": "https://t.me/example",
                "service_name": "G_BOOST_90",
                "total_count": 2,
            },
        )
    assert event_id is not None

    deliverer = SmmPanelOutboxDeliverer(make_api(lambda request: httpx.Response(200, json={"status": "pending"})))
    worker = OutboxWorker(worker_config(), sessions, deliverer)

    assert await worker.tick() == 1

    async with sessions() as session:
        purchase = await session.get(Purchase, purchase_id)
        message = await session.get(OutboxMessage, event_id)
    assert purchase is not None
    assert purchase.status == "OUTBOX_DELIVERY_ERROR"
    assert "invalid successful response" in str(purchase.supplier_status)
    assert message is not None
    assert message.status == OutboxStatus.DEAD
    await deliverer.close()


async def test_invalid_payload_without_purchase_reference_still_becomes_dead(sessions):
    async with sessions.begin() as session:
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={"url": "https://t.me/example", "service_name": "G_BOOST_90", "total_count": 2},
        )
    assert event_id is not None

    deliverer = SmmPanelOutboxDeliverer(make_api(lambda request: httpx.Response(200, json={"id": 417})))
    worker = OutboxWorker(worker_config(), sessions, deliverer)

    assert await worker.tick() == 1

    async with sessions() as session:
        message = await session.get(OutboxMessage, event_id)
    assert message is not None
    assert message.status == OutboxStatus.DEAD
    assert "purchase_id" in str(message.last_error)
    await deliverer.close()


async def test_missing_purchase_rolls_back_outbox_completion(sessions):
    async with sessions.begin() as session:
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={
                "purchase_id": 999,
                "url": "https://t.me/example",
                "service_name": "G_BOOST_90",
                "total_count": 2,
            },
        )
    assert event_id is not None
    async with sessions.begin() as session:
        event = (await outbox_repo.claim_due(session, limit=1, lease_seconds=60))[0]

    with pytest.raises(RuntimeError, match="Purchase 999"):
        async with sessions.begin() as session:
            await outbox_repo.complete_smm_panel_delivery(
                session,
                event.id,
                event.lease_token,
                purchase_id=999,
                supplier_order_id="417",
            )

    async with sessions() as session:
        message = await session.get(OutboxMessage, event_id)
    assert message is not None
    assert message.status == OutboxStatus.PENDING
    assert message.attempts == 0
    assert message.lease_token == event.lease_token


async def test_missing_purchase_rolls_back_outbox_failure(sessions):
    async with sessions.begin() as session:
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={"purchase_id": 999},
        )
    assert event_id is not None
    async with sessions.begin() as session:
        event = (await outbox_repo.claim_due(session, limit=1, lease_seconds=60))[0]

    with pytest.raises(RuntimeError, match="Purchase 999"):
        async with sessions.begin() as session:
            await outbox_repo.complete_smm_panel_failure(
                session,
                event.id,
                event.lease_token,
                purchase_id=999,
                error="HTTP 422",
            )

    async with sessions() as session:
        message = await session.get(OutboxMessage, event_id)
    assert message is not None
    assert message.status == OutboxStatus.PENDING
    assert message.attempts == 0
    assert message.lease_token == event.lease_token


async def test_stale_success_does_not_change_purchase(sessions):
    async with sessions.begin() as session:
        purchase = Purchase(platform="ggsel", unique_code="CODE-1", supplier="smm_panel", status="OUTBOX_PENDING")
        session.add(purchase)
        await session.flush()
        purchase_id = purchase.id
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={"purchase_id": purchase_id},
        )
    assert event_id is not None
    async with sessions.begin() as session:
        event = (await outbox_repo.claim_due(session, limit=1, lease_seconds=60))[0]
        await session.execute(update(OutboxMessage).where(OutboxMessage.id == event.id).values(lease_token="newer-lease"))

    async with sessions.begin() as session:
        updated = await outbox_repo.complete_smm_panel_delivery(
            session,
            event.id,
            event.lease_token,
            purchase_id,
            "417",
        )

    async with sessions() as session:
        purchase = await session.get(Purchase, purchase_id)
    assert updated is False
    assert purchase is not None
    assert purchase.status == "OUTBOX_PENDING"
    assert purchase.supplier_order_id is None


async def test_stale_dead_result_does_not_change_purchase(sessions):
    async with sessions.begin() as session:
        purchase = Purchase(platform="ggsel", unique_code="CODE-1", supplier="smm_panel", status="OUTBOX_PENDING")
        session.add(purchase)
        await session.flush()
        purchase_id = purchase.id
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={"purchase_id": purchase_id},
        )
    assert event_id is not None
    async with sessions.begin() as session:
        event = (await outbox_repo.claim_due(session, limit=1, lease_seconds=60))[0]
        await session.execute(update(OutboxMessage).where(OutboxMessage.id == event.id).values(lease_token="newer-lease"))

    async with sessions.begin() as session:
        updated = await outbox_repo.complete_smm_panel_failure(
            session,
            event.id,
            event.lease_token,
            purchase_id,
            "HTTP 422",
        )

    async with sessions() as session:
        purchase = await session.get(Purchase, purchase_id)
    assert updated is False
    assert purchase is not None
    assert purchase.status == "OUTBOX_PENDING"
    assert purchase.supplier_status is None
