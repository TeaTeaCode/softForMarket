from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config.settings import settings
from app.db.models import Base, OutboxMessage, OutboxStatus


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {settings.POSTGRES_SCHEMA}")
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db_session:
        yield db_session
    await engine.dispose()


async def test_outbox_message_defaults_to_pending(session):
    message = OutboxMessage(
        source="ggsel",
        order_key="CODE-1",
        action="create-supplier-order",
        supplier="smm_panel",
        payload={"service_name": "G_BOOST_90", "url": "https://t.me/example", "total_count": 1},
    )
    session.add(message)
    await session.commit()
    await session.refresh(message)

    assert message.status == OutboxStatus.PENDING
    assert message.attempts == 0
    assert isinstance(message.next_attempt_at, datetime)
    assert isinstance(message.created_at, datetime)
    assert message.delivered_at is None
    assert message.lease_token is None


async def test_outbox_delivery_key_is_unique(session):
    values = {
        "source": "digiseller",
        "order_key": "777",
        "action": "create-supplier-order",
        "supplier": "fragment",
        "payload": {"kind": "stars", "username": "durov", "quantity": 50},
    }
    session.add(OutboxMessage(**values))
    await session.commit()

    session.add(OutboxMessage(**values))
    with pytest.raises(IntegrityError):
        await session.commit()
