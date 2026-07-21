from datetime import UTC, datetime, timedelta
import time
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config.settings import settings
from app.db.models import (
    GgselChat,
    Inflight,
    InflightInvoice,
    Notified,
    ProcessedInvoice,
    Purchase,
    SupplierOrder,
)


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


# ─── purchases ──────────────────────────────────────────────────────────────


async def insert_purchase(session: AsyncSession, row: dict[str, Any]) -> None:
    await session.execute(insert(Purchase).values(**row))
    await session.commit()


async def get_by_unique_code(session: AsyncSession, unique_code: str) -> dict[str, Any] | None:
    result = await session.execute(select(Purchase).where(Purchase.unique_code == unique_code))
    obj = result.scalars().first()
    if obj is None:
        return None
    return {c.name: getattr(obj, c.name) for c in Purchase.__table__.columns}


async def get_pending_orders(session: AsyncSession, limit: int = 200) -> list[dict[str, Any]]:
    """Заказы, принятые поставщиком: у них есть order_id, но статус мог не дойти до финала."""
    # протухшие отсеиваем в SQL, иначе они занимают весь батч и вытесняют живые заказы
    cutoff = datetime.now(UTC) - timedelta(hours=settings.ORDER_POLL_MAX_AGE_HOURS)
    min_created = cutoff.astimezone().isoformat(timespec="seconds")  # created_at — ISO-строка, см. now_iso
    result = await session.execute(
        select(Purchase)
        .where(Purchase.status == "SUPPLIER_ACCEPTED")
        .where(Purchase.supplier_order_id.is_not(None))
        .where(Purchase.created_at >= min_created)
        .order_by(Purchase.id.desc())
        .limit(limit)
    )
    return [{c.name: getattr(obj, c.name) for c in Purchase.__table__.columns} for obj in result.scalars().all()]


async def update_supplier_by_ucode(
    session: AsyncSession, unique_code: str, status: str | None = None, order_id: str | None = None
) -> None:
    values: dict[str, Any] = {}
    if order_id is not None:
        values["supplier_order_id"] = order_id
    if status is not None:
        values["supplier_status"] = status
    if values:
        await session.execute(update(Purchase).where(Purchase.unique_code == unique_code).values(**values))
        await session.commit()


async def mark_order_done(session: AsyncSession, unique_code: str) -> None:
    """Заказ закрыт и уведомление ушло — снимаем с опроса, иначе поллер тянет его вечно."""
    await session.execute(
        update(Purchase).where(Purchase.unique_code == unique_code, Purchase.status == "SUPPLIER_ACCEPTED").values(status="DONE")
    )
    await session.commit()


async def update_supplier_by_inv(
    session: AsyncSession, inv: int, status: str | None = None, order_id: str | None = None
) -> None:
    values: dict[str, Any] = {}
    if order_id is not None:
        values["supplier_order_id"] = order_id
    if status is not None:
        values["supplier_status"] = status
    if values:
        await session.execute(update(Purchase).where(Purchase.inv == inv).values(**values))
        await session.commit()


# ─── inflight по unique_code (GGSEL) ─────────────────────────────────────────


async def mark_inflight(session: AsyncSession, unique_code: str) -> bool:
    """True — блокировка взята; False — уже обрабатывается другим запросом."""
    now = int(time.time())
    await session.execute(delete(Inflight).where(Inflight.expires_at < now))
    exists = (await session.execute(select(Inflight).where(Inflight.unique_code == unique_code))).scalars().first()
    if exists is not None:
        await session.commit()
        return False
    await session.execute(insert(Inflight).values(unique_code=unique_code, expires_at=now + settings.INFLIGHT_TTL_SECONDS))
    await session.commit()
    return True


async def clear_inflight(session: AsyncSession, unique_code: str) -> None:
    await session.execute(delete(Inflight).where(Inflight.unique_code == unique_code))
    await session.commit()


# ─── inflight / processed по inv (Digiseller) ────────────────────────────────


async def is_processed(session: AsyncSession, inv: int) -> bool:
    result = await session.execute(select(ProcessedInvoice).where(ProcessedInvoice.inv == inv))
    return result.scalars().first() is not None


async def mark_processed(session: AsyncSession, inv: int) -> None:
    stmt = pg_insert(ProcessedInvoice).values(inv=inv).on_conflict_do_nothing(index_elements=["inv"])
    await session.execute(stmt)
    await session.commit()


async def try_acquire_inflight_inv(session: AsyncSession, inv: int) -> bool:
    """True — блокировка взята; False — ещё активна. Просроченную (по TTL) перехватываем."""
    result = await session.execute(select(InflightInvoice.created_at).where(InflightInvoice.inv == inv))
    created = result.scalars().first()
    if created is not None:
        # наивное время трактуем как UTC
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - created).total_seconds()
        if age < settings.INFLIGHT_TTL_SECONDS:
            return False
        await session.execute(delete(InflightInvoice).where(InflightInvoice.inv == inv))
    stmt = pg_insert(InflightInvoice).values(inv=inv).on_conflict_do_nothing(index_elements=["inv"])
    await session.execute(stmt)
    await session.commit()
    return True


async def release_inflight_inv(session: AsyncSession, inv: int) -> None:
    await session.execute(delete(InflightInvoice).where(InflightInvoice.inv == inv))
    await session.commit()


# ─── supplier_orders ─────────────────────────────────────────────────────────


async def save_supplier_order(session: AsyncSession, inv: int, order_id: str) -> None:
    stmt = (
        pg_insert(SupplierOrder)
        .values(inv=inv, order_id=str(order_id))
        .on_conflict_do_update(index_elements=["inv"], set_={"order_id": str(order_id)})
    )
    await session.execute(stmt)
    await session.commit()


async def get_supplier_order(session: AsyncSession, inv: int) -> str | None:
    result = await session.execute(select(SupplierOrder.order_id).where(SupplierOrder.inv == inv))
    return result.scalars().first()


# ─── notified (анти-спам) ────────────────────────────────────────────────────


async def try_mark_notified(session: AsyncSession, unique_code: str, kind: str) -> bool:
    """True — уведомление ещё не отправлялось (можно слать); False — уже было."""
    if not unique_code:
        return True
    result = await session.execute(select(Notified).where(Notified.unique_code == unique_code, Notified.kind == kind))
    if result.scalars().first() is not None:
        return False
    await session.execute(insert(Notified).values(unique_code=unique_code, kind=kind))
    await session.commit()
    return True


async def is_notified(session: AsyncSession, unique_code: str, kind: str) -> bool:
    result = await session.execute(select(Notified).where(Notified.unique_code == unique_code, Notified.kind == kind))
    return result.scalars().first() is not None


async def unmark_notified(session: AsyncSession, unique_code: str, kind: str) -> None:
    """Откат метки: отправка не удалась."""
    await session.execute(delete(Notified).where(Notified.unique_code == unique_code, Notified.kind == kind))
    await session.commit()


# ─── ggsel_chats ─────────────────────────────────────────────────────────────


async def ggsel_chat_get_last_msg_id(session: AsyncSession, id_i: int) -> int | None:
    result = await session.execute(select(GgselChat.last_msg_id).where(GgselChat.id_i == id_i))
    return result.scalars().first()


async def ggsel_chat_set_last_msg_id(session: AsyncSession, id_i: int, msg_id: int) -> None:
    stmt = (
        pg_insert(GgselChat)
        .values(id_i=int(id_i), last_msg_id=int(msg_id))
        .on_conflict_do_update(index_elements=["id_i"], set_={"last_msg_id": int(msg_id)})
    )
    await session.execute(stmt)
    await session.commit()
