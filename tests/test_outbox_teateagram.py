from unittest.mock import patch
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clients.suppliers.smm_panel import SmmPanelApi
from app.clients.suppliers.teateagram import TeaTeaGramApi
from app.core.config.settings import settings
from app.db import outbox_repository as outbox_repo
from app.db.models import Base, OutboxMessage, OutboxStatus, Purchase
from app.services.outbox.delivery import DeliveryOutcome, NonRetryableDeliveryError, OutboxRouter, classify_delivery_error
from app.services.outbox.smm_panel import SmmPanelOutboxDeliverer
from app.services.outbox.teateagram import TeaTeaGramOutboxDeliverer
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
    api = TeaTeaGramApi()
    api._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
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


def event(supplier="teateagram", payload=None):
    return outbox_repo.DueOutboxMessage(
        id=1,
        source="ggsel",
        order_key="CODE-1",
        action="create-supplier-order",
        supplier=supplier,
        payload=payload or {"purchase_id": 23, "service": 114, "link": "https://t.me/example", "quantity": 2},
        attempts=0,
        lease_token="lease",
    )


async def enqueue_purchase(sessions, supplier="teateagram"):
    async with sessions.begin() as session:
        purchase = Purchase(platform="ggsel", unique_code="CODE-1", supplier=supplier, status="OUTBOX_PENDING")
        session.add(purchase)
        await session.flush()
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier=supplier,
            payload={"purchase_id": purchase.id, "service": 114, "link": "https://t.me/example", "quantity": 2},
        )
    assert event_id is not None
    return purchase.id, event_id


async def test_default_deliverer_disables_hidden_http_transport_retries():
    deliverer = TeaTeaGramOutboxDeliverer()

    assert deliverer._client._transport_retry_total == 0
    await deliverer.close()


@pytest.mark.parametrize("order", [417, "417"])
async def test_deliverer_sends_socpanel_add_and_extracts_order_id(order):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["form"] = parse_qs(request.content.decode())
        return httpx.Response(200, json={"order": order})

    deliverer = TeaTeaGramOutboxDeliverer(make_api(handler))

    result = await deliverer.deliver(event())

    assert seen["url"] == "https://teateagram.com/api/v2"
    assert seen["form"]["action"] == ["add"]
    assert seen["form"]["service"] == ["114"]
    assert seen["form"]["link"] == ["https://t.me/example"]
    assert seen["form"]["quantity"] == ["2"]
    assert result.supplier_order_id == "417"
    await deliverer.close()


async def test_socpanel_error_body_is_final_without_retry():
    api = make_api(lambda request: httpx.Response(200, json={"error": "Incorrect service ID"}))
    deliverer = TeaTeaGramOutboxDeliverer(api)

    with pytest.raises(NonRetryableDeliveryError, match="Incorrect service ID") as exc_info:
        await deliverer.deliver(event())

    assert classify_delivery_error(exc_info.value) == DeliveryOutcome.DEAD
    await deliverer.close()


async def test_worker_persists_socpanel_order_and_schedules_status_check(sessions):
    purchase_id, event_id = await enqueue_purchase(sessions)
    deliverer = TeaTeaGramOutboxDeliverer(make_api(lambda request: httpx.Response(200, json={"order": 417})))
    worker = OutboxWorker(worker_config(), sessions, OutboxRouter({"teateagram": deliverer}))

    with patch("app.services.outbox.teateagram.background.schedule_outbox_status_check") as schedule:
        assert await worker.tick() == 1

    async with sessions() as session:
        purchase = await session.get(Purchase, purchase_id)
        message = await session.get(OutboxMessage, event_id)
    assert purchase is not None
    assert purchase.supplier == "teateagram"
    assert purchase.supplier_order_id == "417"
    assert purchase.status == "SUPPLIER_ACCEPTED"
    assert message is not None
    assert message.status == OutboxStatus.DELIVERED
    schedule.assert_called_once_with("CODE-1", "417", {"order": 417}, "teateagram")
    await deliverer.close()


async def test_worker_marks_purchase_failed_on_socpanel_error(sessions):
    purchase_id, event_id = await enqueue_purchase(sessions)
    api = make_api(lambda request: httpx.Response(200, json={"error": "Not enough funds on balance"}))
    deliverer = TeaTeaGramOutboxDeliverer(api)
    worker = OutboxWorker(worker_config(), sessions, OutboxRouter({"teateagram": deliverer}))

    assert await worker.tick() == 1

    async with sessions() as session:
        purchase = await session.get(Purchase, purchase_id)
        message = await session.get(OutboxMessage, event_id)
    assert purchase is not None
    assert purchase.status == "OUTBOX_DELIVERY_ERROR"
    assert "Not enough funds" in str(purchase.supplier_status)
    assert message is not None
    assert message.status == OutboxStatus.DEAD
    await deliverer.close()


async def test_router_delivers_queued_gaba_orders_after_switch(sessions):
    async with sessions.begin() as session:
        purchase = Purchase(platform="ggsel", unique_code="CODE-1", supplier="smm_panel", status="OUTBOX_PENDING")
        session.add(purchase)
        await session.flush()
        await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="smm_panel",
            payload={"purchase_id": purchase.id, "url": "https://t.me/x", "service_name": "G_BOOST_90", "total_count": 1},
        )
    teateagram_calls = []
    socpanel = TeaTeaGramOutboxDeliverer(make_api(lambda request: teateagram_calls.append(request) or httpx.Response(500)))
    gaba_api = SmmPanelApi()
    gaba_api._client = httpx.AsyncClient(
        base_url="https://supplier.test/api/v1/external",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"id": 9})),
    )
    gaba = SmmPanelOutboxDeliverer(gaba_api)
    router = OutboxRouter({"teateagram": socpanel, "smm_panel": gaba})

    with patch("app.services.outbox.smm_panel.background.schedule_outbox_status_check"):
        assert await OutboxWorker(worker_config(), sessions, router).tick() == 1

    async with sessions() as session:
        stored = await session.get(Purchase, purchase.id)
    assert stored is not None
    assert stored.supplier == "smm_panel"
    assert stored.supplier_order_id == "9"
    assert teateagram_calls == []
    await router.close()


async def test_router_marks_unknown_supplier_dead(sessions):
    async with sessions.begin() as session:
        event_id = await outbox_repo.enqueue(
            session,
            source="ggsel",
            order_key="CODE-1",
            action="create-supplier-order",
            supplier="mystery",
            payload={"purchase_id": 1},
        )
    router = OutboxRouter({})

    assert await OutboxWorker(worker_config(), sessions, router).tick() == 1

    async with sessions() as session:
        message = await session.get(OutboxMessage, event_id)
    assert message is not None
    assert message.status == OutboxStatus.DEAD
    assert "mystery" in str(message.last_error)
