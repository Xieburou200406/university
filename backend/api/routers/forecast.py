# -*- coding: utf-8 -*-
"""/api/forecast —— 隔夜开盘方向预测（§13）。abstain 是一等公民；晨检只修置信度不推翻方向。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db
from db.models import OpenForecast


class ForecastOut(BaseModel):
    date: date
    model_ver: str
    p_up_raw: float | None
    p_up_cal: float | None
    abstain: bool
    reason: str | None
    features_json: dict | None
    brier_60d: float | None
    morning_adj: float | None

    class Config:
        from_attributes = True


class MorningCheckIn(BaseModel):
    p_up_observed: float   # 9:25 竞价/晨间数据修正后的 P(up)


def router() -> APIRouter:
    r = APIRouter()

    @r.get("/forecast/next-open", response_model=ForecastOut)
    def next_open(db: Session = Depends(get_db)):
        row = db.execute(select(OpenForecast).order_by(OpenForecast.date.desc())
                         .limit(1)).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "尚无预测记录（先触发管线）")
        return row

    @r.get("/forecast/history", response_model=list[ForecastOut])
    def history(limit: int = Query(default=30, le=260), db: Session = Depends(get_db)):
        return db.execute(select(OpenForecast).order_by(OpenForecast.date.desc())
                          .limit(limit)).scalars().all()

    @r.post("/forecast/morning-check/{d}", response_model=ForecastOut)
    def morning_check(d: date, body: MorningCheckIn, db: Session = Depends(get_db)):
        """9:25 竞价复核（§13.1 双时点）：写 morning_adj，不改 p_up_cal（不推翻 T 收盘结论）。"""
        row = db.get(OpenForecast, d)
        if row is None:
            raise HTTPException(404, f"{d} 无预测记录")
        row.morning_adj = round(body.p_up_observed, 4)
        db.flush()
        return row

    return r
