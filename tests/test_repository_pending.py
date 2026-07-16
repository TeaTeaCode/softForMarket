from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config.settings import settings
from app.db import repository as repo
from app.db.models import Base, Purchase


def iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).astimezone().isoformat(timespec="seconds")


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        # модели живут в схеме marketplace — в sqlite её изображаем присоединённой БД
        await conn.exec_driver_sql(f"ATTACH DATABASE ':memory:' AS {settings.POSTGRES_SCHEMA}")
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as s:
        yield s
    await engine.dispose()


async def add(session, code, hours_ago, status="SUPPLIER_ACCEPTED", order_id="ord-1"):
    session.add(
        Purchase(unique_code=code, status=status, supplier_order_id=order_id, created_at=iso(hours_ago), platform="ggsel")
    )
    await session.commit()


async def codes(session, limit=200):
    return [r["unique_code"] for r in await repo.get_pending_orders(session, limit)]


async def test_stale_orders_excluded(session):
    age = settings.ORDER_POLL_MAX_AGE_HOURS
    await add(session, "fresh", 1)
    await add(session, "stale", age + 24)

    assert await codes(session) == ["fresh"]


async def test_stale_orders_do_not_consume_batch(session):
    """Протухшие не должны вытеснять живые заказы из лимита."""
    age = settings.ORDER_POLL_MAX_AGE_HOURS
    for i in range(5):
        await add(session, f"stale-{i}", age + 24 + i)
    await add(session, "fresh", 1)

    # лимит меньше числа протухших: без SQL-фильтра живой заказ сюда бы не попал
    assert await codes(session, limit=3) == ["fresh"]


async def test_order_without_supplier_id_excluded(session):
    await add(session, "no-order-id", 1, order_id=None)
    assert await codes(session) == []


async def test_non_accepted_status_excluded(session):
    await add(session, "errored", 1, status="ERROR")
    assert await codes(session) == []
