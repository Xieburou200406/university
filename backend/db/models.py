# -*- coding: utf-8 -*-
"""SQLAlchemy 模型 —— 详细设计 §5 的正式版 schema（data/vol.db，独立于 demo 的 vol_demo.db）。
约定：日期列用 Date；JSON 列只透传展示不参与查询；量纲与全库一致（iv 小数、vega 每 1.0 IV、theta 年化）。"""
from datetime import date as Date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DailyMetric(Base):
    """每日市场体检表（类比引擎的特征来源）。"""
    __tablename__ = "daily_metric"

    date: Mapped[Date] = mapped_column(Date, primary_key=True)
    spot: Mapped[float] = mapped_column(Float)
    atm_iv: Mapped[float] = mapped_column(Float)                 # 小数
    iv_pct_40: Mapped[float | None] = mapped_column(Float, nullable=True)   # 展示对照用
    iv_pct_120: Mapped[float | None] = mapped_column(Float, nullable=True)  # 正式信号口径
    curve_model: Mapped[str | None] = mapped_column(String(16), nullable=True)
    curve_r2: Mapped[float | None] = mapped_column(Float, nullable=True)
    skew_25d: Mapped[float | None] = mapped_column(Float, nullable=True)
    term_slope: Mapped[float | None] = mapped_column(Float, nullable=True)
    vrp: Mapped[float | None] = mapped_column(Float, nullable=True)
    momentum_20d: Mapped[float | None] = mapped_column(Float, nullable=True)


class Signal(Base):
    """信号流水。valid_until = T+1（铁律 8：过期作废）。"""
    __tablename__ = "signal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[Date] = mapped_column(Date, index=True)
    engine: Mapped[str] = mapped_column(String(16))              # percentile / analog / ensemble
    signal: Mapped[str] = mapped_column(String(32))              # SHORT_VOL / LONG_VOL / NEUTRAL / WAIT
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    valid_until: Mapped[Date | None] = mapped_column(Date, nullable=True)
    detail_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class AdviceCard(Base):
    """建议卡片存档（审计追溯）。"""
    __tablename__ = "advice_card"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signal.id"), index=True)
    verdict_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)   # 风控链 trace
    card_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Position(Base):
    """模拟持仓账本。status: open / closed。"""
    __tablename__ = "position"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32))
    qty: Mapped[int] = mapped_column(Integer)                    # 正=多头，负=空头
    open_date: Mapped[Date] = mapped_column(Date)
    open_px: Mapped[float] = mapped_column(Float)
    close_date: Mapped[Date | None] = mapped_column(Date, nullable=True)
    close_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(String(8), default="open")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class PositionSnapshot(Base):
    """每日持仓估值快照 —— 归因的唯一输入（快照差分，禁止累计值直接拆解）。"""
    __tablename__ = "position_snapshot"
    __table_args__ = (UniqueConstraint("date", "position_id", name="uq_snap_date_pos"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[Date] = mapped_column(Date, index=True)
    position_id: Mapped[int] = mapped_column(ForeignKey("position.id"))
    mv: Mapped[float] = mapped_column(Float)                     # 市值（元）
    delta: Mapped[float] = mapped_column(Float)
    vega: Mapped[float] = mapped_column(Float)
    theta: Mapped[float] = mapped_column(Float)                  # 年化口径
    iv: Mapped[float] = mapped_column(Float)
    spot: Mapped[float] = mapped_column(Float)


class ReviewLog(Base):
    """周度复核（weekly_review 产物）。"""
    __tablename__ = "review_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    week: Mapped[str] = mapped_column(String(10))                # 如 2026-W41
    sample_n: Mapped[int] = mapped_column(Integer)
    hit_rate: Mapped[float] = mapped_column(Float)
    baseline: Mapped[float] = mapped_column(Float)
    by_regime_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    warn: Mapped[bool] = mapped_column(Boolean, default=False)   # 跌破基准告警


class PipelineRun(Base):
    """管线运行记录（可观测性 + 防重入锁：status=running 即拒绝新触发）。"""
    __tablename__ = "pipeline_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")   # running/ok/failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    rows_fetched: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PositionReconcile(Base):
    """持仓两源对账（详细设计 §11.2）：手动录入 vs 券商对账单。"""
    __tablename__ = "position_reconcile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[Date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(16))              # manual / broker_stmt
    per_contract_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16))              # all_match / net_match / mismatch


class ExecutionLog(Base):
    """执行质量回顾（详细设计 §11.3）：实现滑点 vs 回测成本假设。"""
    __tablename__ = "execution_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signal.id"), index=True)
    acted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    filled_px: Mapped[float | None] = mapped_column(Float, nullable=True)
    slippage_bp: Mapped[float | None] = mapped_column(Float, nullable=True)


class OpenForecast(Base):
    """隔夜开盘方向预测（§13.4）。abstain 是一等公民；brier_60d 质量自监控。"""
    __tablename__ = "open_forecast"

    date: Mapped[Date] = mapped_column(Date, primary_key=True)   # 预测发出日（T）
    model_ver: Mapped[str] = mapped_column(String(16), default="lr-irls-v1")
    p_up_raw: Mapped[float | None] = mapped_column(Float, nullable=True)
    p_up_cal: Mapped[float | None] = mapped_column(Float, nullable=True)
    abstain: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    features_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    brier_60d: Mapped[float | None] = mapped_column(Float, nullable=True)
    morning_adj: Mapped[float | None] = mapped_column(Float, nullable=True)  # 9:25 竞价修正后的 P(up)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class RiskBudget(Base):
    """今日最优风险预算（§14.4）。目标敞口为区间中心，区间宽度由置信度决定。"""
    __tablename__ = "risk_budget"

    date: Mapped[Date] = mapped_column(Date, primary_key=True)
    lambda_level: Mapped[str] = mapped_column(String(8))          # conservative / balanced / aggressive
    target_vega: Mapped[float] = mapped_column(Float)
    target_vega_lo: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_vega_hi: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_delta: Mapped[float] = mapped_column(Float)
    target_delta_lo: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_delta_hi: Mapped[float | None] = mapped_column(Float, nullable=True)
    cur_vega: Mapped[float] = mapped_column(Float)
    cur_delta: Mapped[float] = mapped_column(Float)
    cvar5: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[str] = mapped_column(String(8), default="LOW")
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    notes_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
