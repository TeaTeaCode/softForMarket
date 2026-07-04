from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Float, Integer, String, func
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
    """Блокировка обработки заказа по unique_code (GGSEL). expires_at — unix-время."""

    __tablename__ = "inflight"

    unique_code: Mapped[str] = mapped_column(String, primary_key=True)
    expires_at: Mapped[int] = mapped_column(Integer)


class ProcessedInvoice(Base):
    __tablename__ = "processed_invoices"

    inv: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class InflightInvoice(Base):
    """Блокировка обработки заказа по inv (Digiseller)."""

    __tablename__ = "inflight_invoices"

    inv: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class SupplierOrder(Base):
    __tablename__ = "supplier_orders"

    inv: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class Notified(Base):
    """Анти-спам уведомлений: одно уведомление каждого вида (success/fail) на unique_code."""

    __tablename__ = "notified"

    unique_code: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())


class GgselChat(Base):
    """Последнее пересланное сообщение чата GGSEL (для поллера)."""

    __tablename__ = "ggsel_chats"

    id_i: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    last_msg_id: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.current_timestamp())
