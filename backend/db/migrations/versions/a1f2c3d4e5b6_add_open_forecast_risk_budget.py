"""add open_forecast + risk_budget (§13.4 / §14.4)

Revision ID: a1f2c3d4e5b6
Revises: d90094c083ad
Create Date: 2026-10-09 09:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1f2c3d4e5b6'
down_revision: Union[str, Sequence[str], None] = 'd90094c083ad'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('open_forecast',
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('model_ver', sa.String(length=16), nullable=False),
        sa.Column('p_up_raw', sa.Float(), nullable=True),
        sa.Column('p_up_cal', sa.Float(), nullable=True),
        sa.Column('abstain', sa.Boolean(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('features_json', sa.JSON(), nullable=True),
        sa.Column('brier_60d', sa.Float(), nullable=True),
        sa.Column('morning_adj', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('date'),
    )
    op.create_table('risk_budget',
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('lambda_level', sa.String(length=8), nullable=False),
        sa.Column('target_vega', sa.Float(), nullable=False),
        sa.Column('target_vega_lo', sa.Float(), nullable=True),
        sa.Column('target_vega_hi', sa.Float(), nullable=True),
        sa.Column('target_delta', sa.Float(), nullable=False),
        sa.Column('target_delta_lo', sa.Float(), nullable=True),
        sa.Column('target_delta_hi', sa.Float(), nullable=True),
        sa.Column('cur_vega', sa.Float(), nullable=False),
        sa.Column('cur_delta', sa.Float(), nullable=False),
        sa.Column('cvar5', sa.Float(), nullable=True),
        sa.Column('confidence', sa.String(length=8), nullable=False),
        sa.Column('degraded', sa.Boolean(), nullable=False),
        sa.Column('notes_json', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('date'),
    )


def downgrade() -> None:
    op.drop_table('risk_budget')
    op.drop_table('open_forecast')
