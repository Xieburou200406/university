"""advice_card lifecycle columns (status / adopted_at, §18.6 滞留-采纳闭环)

Revision ID: b4c5d6e7f8a9
Revises: a1f2c3d4e5b6
Create Date: 2026-10-09 14:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, Sequence[str], None] = 'a1f2c3d4e5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('advice_card',
                  sa.Column('status', sa.String(length=16), nullable=False,
                            server_default='pending'))
    op.add_column('advice_card',
                  sa.Column('adopted_at', sa.DateTime(), nullable=True))
    op.create_index('ix_advice_card_status', 'advice_card', ['status'])


def downgrade() -> None:
    op.drop_index('ix_advice_card_status', table_name='advice_card')
    op.drop_column('advice_card', 'adopted_at')
    op.drop_column('advice_card', 'status')
