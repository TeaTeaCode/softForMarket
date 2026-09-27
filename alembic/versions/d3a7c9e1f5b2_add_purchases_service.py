"""add purchases.service

Revision ID: d3a7c9e1f5b2
Revises: b8f0e3a1c6d2
Create Date: 2026-09-27 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd3a7c9e1f5b2'
down_revision: Union[str, Sequence[str], None] = 'b8f0e3a1c6d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('purchases', sa.Column('service', sa.String(), nullable=True), schema='marketplace')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('purchases', 'service', schema='marketplace')
