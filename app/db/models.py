from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Enum, Float, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class Purchase(Base):
    __tablename__ = "purchases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str | None] = mapped_column(String, index=True)
    unique_code: Mapped[str | None] = mapped_column(String, index=True)
    inv: Mapped[int | None] = mapped_column(BigInteger, index=True)
    goods_id: Mapped[str | None] = mapped_column(String)
    amount: Mapped[float | None] = mapped_column(Float)
    amount_usd: Mapped[float | None] = mapped_column(Float)
    profit: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String)
    tg_link: Mapped[str | None] = mapped_column(String)
    days: Mapped[int | None] = mapped_column(Integer)
    quantity: Mapped[int | None] = mapped_column(Integer)
    supplier: Mapped[str | None] = mapped_column(String)  # 'teateagram' | 'smm_panel'
    supplier_order_id: Mapped[str | None] = mapped_column(String)
    supplier_status: Mapped[str | None] = mapped_column(String)
    status: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[str | None] = mapped_column(String, index=True)


class Inflight(Base):
    __tablename__ = "inflight"

    unique_code: Mapped[str] = mapped_column(String, primary_key=True)
    expires_at: Mapped[int] = mapped_column(Integer)  # unix-время


class ProcessedInvoice(Base):
    __tablename__ = "processed_invoices"

    inv: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class InflightInvoice(Base):
    __tablename__ = "inflight_invoices"

    inv: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class SupplierOrder(Base):
    __tablename__ = "supplier_orders"

    inv: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Notified(Base):
    __tablename__ = "notified"

    unique_code: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)  # success | fail
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class GgselChat(Base):
    __tablename__ = "ggsel_chats"

    id_i: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_msg_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class OutboxStatus(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    DEAD = "dead"


class OutboxMessage(Base):
    __tablename__ = "outbox_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    order_key: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    supplier: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(
            OutboxStatus,
            name="outbox_status",
            schema=Base.metadata.schema,
            values_callable=lambda enum: [item.value for item in enum],
        ),
        nullable=False,
        default=OutboxStatus.PENDING,
        server_default=OutboxStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    lease_token: Mapped[str | None] = mapped_column(String(36))
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("source", "order_key", "action", name="uq_outbox_delivery"),
        Index("ix_outbox_due", "status", "next_attempt_at"),
    )
