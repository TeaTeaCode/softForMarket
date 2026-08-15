"""create outbox messages

Revision ID: b8f0e3a1c6d2
Revises: 7c41f2a9d3b8
Create Date: 2026-08-15 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b8f0e3a1c6d2"
down_revision: Union[str, Sequence[str], None] = "7c41f2a9d3b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DB_SCHEMA = "marketplace"


def upgrade() -> None:
    outbox_status = postgresql.ENUM("pending", "delivered", "dead", name="outbox_status", schema=DB_SCHEMA)
    outbox_status.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "outbox_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("order_key", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("supplier", sa.String(length=32), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "delivered",
                "dead",
                name="outbox_status",
                schema=DB_SCHEMA,
                create_type=False,
            ),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("lease_token", sa.String(length=36), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source", "order_key", "action", name="uq_outbox_delivery"),
        schema=DB_SCHEMA,
    )
    op.create_index(
        "ix_outbox_due",
        "outbox_messages",
        ["status", "next_attempt_at"],
        unique=False,
        schema=DB_SCHEMA,
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_due", table_name="outbox_messages", schema=DB_SCHEMA)
    op.drop_table("outbox_messages", schema=DB_SCHEMA)
    postgresql.ENUM(name="outbox_status", schema=DB_SCHEMA).drop(op.get_bind(), checkfirst=True)
