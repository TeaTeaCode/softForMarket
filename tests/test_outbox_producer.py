import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config.settings import settings
from app.db.models import Base, OutboxMessage, Purchase
from app.services.outbox.producer import enqueue_smm_panel_purchase, queue_smm_panel_purchase


@pytest.fixture
async def sessions():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {settings.POSTGRES_SCHEMA}")
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


def purchase_values():
    return {
        "platform": "ggsel",
        "unique_code": "CODE-1",
        "inv": 555,
        "goods_id": "102084952",
        "email": "buyer@example.com",
        "tg_link": "https://t.me/example",
        "days": 90,
        "quantity": 2,
        "supplier": "smm_panel",
        "supplier_order_id": None,
        "status": "OUTBOX_PENDING",
        "created_at": "2026-08-15T12:00:00+03:00",
    }


async def test_purchase_and_outbox_are_created_in_one_transaction(sessions):
    async with sessions() as session:
        queued = await queue_smm_panel_purchase(
            session,
            purchase_values=purchase_values(),
            source="ggsel",
            order_key="CODE-1",
            service_name="G_BOOST_90",
            url="https://t.me/example",
            total_count=2,
        )

    assert queued is not None
    async with sessions() as session:
        purchase = await session.get(Purchase, queued.purchase_id)
        message = await session.get(OutboxMessage, queued.outbox_id)
    assert purchase is not None
    assert purchase.status == "OUTBOX_PENDING"
    assert purchase.supplier == "smm_panel"
    assert message is not None
    assert message.payload == {
        "purchase_id": purchase.id,
        "url": "https://t.me/example",
        "service_name": "G_BOOST_90",
        "total_count": 2,
    }


async def test_transaction_rolls_back_purchase_and_outbox_together(sessions):
    with pytest.raises(RuntimeError, match="abort"):
        async with sessions() as session:
            async with session.begin():
                await enqueue_smm_panel_purchase(
                    session,
                    purchase_values=purchase_values(),
                    source="ggsel",
                    order_key="CODE-1",
                    service_name="G_BOOST_90",
                    url="https://t.me/example",
                    total_count=2,
                )
                raise RuntimeError("abort")

    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Purchase)) == 0
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 0


async def test_duplicate_outbox_key_does_not_create_second_purchase(sessions):
    async with sessions() as session:
        first = await queue_smm_panel_purchase(
            session,
            purchase_values=purchase_values(),
            source="ggsel",
            order_key="CODE-1",
            service_name="G_BOOST_90",
            url="https://t.me/example",
            total_count=2,
        )
    async with sessions() as session:
        duplicate = await queue_smm_panel_purchase(
            session,
            purchase_values=purchase_values(),
            source="ggsel",
            order_key="CODE-1",
            service_name="G_BOOST_90",
            url="https://t.me/example",
            total_count=2,
        )

    assert first is not None
    assert duplicate is None
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Purchase)) == 1
        assert await session.scalar(select(func.count()).select_from(OutboxMessage)) == 1
