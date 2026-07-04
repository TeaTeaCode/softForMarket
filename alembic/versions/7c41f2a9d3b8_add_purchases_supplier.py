"""add purchases.supplier

Revision ID: 7c41f2a9d3b8
Revises: 31be2b76e7a2
Create Date: 2026-07-04 05:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c41f2a9d3b8'
down_revision: Union[str, Sequence[str], None] = '31be2b76e7a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('purchases', sa.Column('supplier', sa.String(), nullable=True), schema='marketplace')
    # все существующие заказы оформлялись через TeaTeaGram
    op.execute(
        "UPDATE marketplace.purchases SET supplier = 'teateagram' WHERE supplier_order_id IS NOT NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('purchases', 'supplier', schema='marketplace')
