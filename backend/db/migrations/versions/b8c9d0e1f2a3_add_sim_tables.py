# -*- coding: utf-8 -*-
"""revision: b8c9d0e1f2a3 (M6-① 模拟盘 5 表)
down_revision: b4c5d6e7f8a9

sim_account / sim_order / sim_fill / sim_nav / sim_session —— §12.3。
模拟盘与真实账本物理隔离（独立前缀 sim_，铁律 10 不受影响：模拟撮合不碰真实账户）。
"""
from alembic import op
import sqlalchemy as sa

revision: str = "b8c9d0e1f2a3"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sim_account",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("auto_mirror", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cash0", sa.Float(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "sim_order",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("sim_account.id"), nullable=False, index=True),
        sa.Column("tpl_id", sa.String(24)),
        sa.Column("params_json", sa.JSON()),
        sa.Column("legs_json", sa.JSON(), nullable=False),
        sa.Column("submit_date", sa.Date(), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("price_type", sa.String(8), nullable=False, server_default="market"),
        sa.Column("limit_px", sa.Float()),
        sa.Column("status", sa.String(8), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "sim_fill",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.Integer(),
                  sa.ForeignKey("sim_order.id"), nullable=False, index=True),
        sa.Column("leg_index", sa.Integer(), nullable=False),
        sa.Column("cp", sa.String(1), nullable=False),
        sa.Column("strike", sa.Float(), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("fill_px", sa.Float(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("filled_date", sa.Date(), nullable=False),
    )
    op.create_table(
        "sim_nav",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("sim_account.id"), nullable=False, index=True),
        sa.Column("ts", sa.Date(), nullable=False),
        sa.Column("nav", sa.Float(), nullable=False),
        sa.Column("mv", sa.Float()),
        sa.UniqueConstraint("account_id", "ts", name="uq_simnav_acct_ts"),
    )
    op.create_table(
        "sim_session",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mode", sa.String(8), nullable=False, server_default="replay"),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("speed", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("summary_json", sa.JSON()),
    )


def downgrade() -> None:
    for t in ("sim_session", "sim_nav", "sim_fill", "sim_order", "sim_account"):
        op.drop_table(t)
