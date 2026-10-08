# -*- coding: utf-8 -*-
"""Pydantic v2 响应/请求模型。字段名与 db.models 一致（from_attributes 直读 ORM）。"""
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- health ----------
class HealthOut(BaseModel):
    status: str
    db: str
    tables: int
    advice_only: bool = True   # 铁律 10：对外明示，本系统永远不自动下单


# ---------- daily_metric ----------
class DailyMetricOut(ORMModel):
    date: date
    spot: float
    atm_iv: float
    iv_pct_40: float | None = None
    iv_pct_120: float | None = None
    curve_model: str | None = None
    curve_r2: float | None = None
    skew_25d: float | None = None
    term_slope: float | None = None
    vrp: float | None = None
    momentum_20d: float | None = None


# ---------- signal ----------
class SignalOut(ORMModel):
    id: int
    date: date
    engine: str
    signal: str
    confidence: float | None = None
    valid_until: date | None = None
    detail_json: dict | None = None
    created_at: datetime


class SignalDetailOut(SignalOut):
    advice_cards: list["AdviceCardOut"] = []


# ---------- advice ----------
class RiskCheck(BaseModel):
    name: str
    passed: bool
    detail: str


class AdviceCardOut(ORMModel):
    id: int
    signal_id: int
    verdict_json: dict | None = None
    card_json: dict | None = None
    created_at: datetime


class AdviceGenerateOut(BaseModel):
    card: AdviceCardOut
    verdict: str                       # PASS / WARN / REJECT
    risk_checks: list[RiskCheck]


# ---------- position ----------
class PositionCreate(BaseModel):
    code: str = Field(min_length=1, description="合约代码，如 510050C2610M02700")
    qty: int = Field(description="正=多头，负=空头")
    open_date: date
    open_px: float = Field(gt=0)
    note: str | None = None


class PositionOut(ORMModel):
    id: int
    code: str
    qty: int
    open_date: date
    open_px: float
    close_date: date | None = None
    close_px: float | None = None
    status: str
    note: str | None = None


class PositionClose(BaseModel):
    close_date: date
    close_px: float = Field(gt=0)


class PositionSnapshotOut(ORMModel):
    date: date
    position_id: int
    mv: float
    delta: float
    vega: float
    theta: float
    iv: float
    spot: float


# ---------- review / pipeline ----------
class ReviewLogOut(ORMModel):
    week: str
    sample_n: int
    hit_rate: float
    baseline: float
    by_regime_json: dict | None = None
    warn: bool


class PipelineRunOut(ORMModel):
    id: int
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    error: str | None = None
    rows_fetched: int | None = None


class PipelineRunTrigger(BaseModel):
    days: int = Field(default=0, ge=0, le=520, description="追加缓存天数；0=只算最新一天")


SignalDetailOut.model_rebuild()
