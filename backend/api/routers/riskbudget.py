# -*- coding: utf-8 -*-
"""/api/riskbudget —— 今日最优风险预算（§14.4）。改 λ 档位 regenerate，幂等。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db, get_settings
from db.models import RiskBudget


class BudgetOut(BaseModel):
    date: date
    lambda_level: str
    target_vega: float
    target_vega_lo: float | None
    target_vega_hi: float | None
    target_delta: float
    target_delta_lo: float | None
    target_delta_hi: float | None
    cur_vega: float
    cur_delta: float
    cvar5: float | None
    confidence: str
    degraded: bool
    notes_json: dict | None

    class Config:
        from_attributes = True


def router() -> APIRouter:
    r = APIRouter()

    @r.get("/riskbudget/today", response_model=BudgetOut)
    def today(db: Session = Depends(get_db)):
        row = db.execute(select(RiskBudget).order_by(RiskBudget.date.desc())
                         .limit(1)).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "尚无预算记录（先触发管线）")
        return row

    @r.post("/riskbudget/regenerate", response_model=BudgetOut)
    def regenerate(lambda_level: str = Query(default="balanced",
                                             pattern="^(conservative|balanced|aggressive)$"),
                   db: Session = Depends(get_db), settings=Depends(get_settings)):
        """改风险厌恶档位后重算今日预算（幂等 upsert，不产生新行）。"""
        from api.services import compute_and_store_budget
        row = db.execute(select(RiskBudget).order_by(RiskBudget.date.desc())
                         .limit(1)).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "尚无预算记录（先触发管线）")
        try:
            from m2_run import build_daily_history
            df = build_daily_history()
        except Exception as e:                       # noqa: BLE001
            raise HTTPException(502, f"面板不可用（fail-closed）：{e}") from e
        return compute_and_store_budget(db, settings, df, lambda_level)

    return r
