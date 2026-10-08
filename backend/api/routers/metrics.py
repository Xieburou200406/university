# -*- coding: utf-8 -*-
"""/api/metrics —— 每日市场体检（只读）。"""
from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import DailyMetricOut
from db.models import DailyMetric


def router() -> APIRouter:
    r = APIRouter()

    @r.get("/metrics", response_model=list[DailyMetricOut])
    def list_metrics(db: Session = Depends(get_db),
                     start: date | None = None, end: date | None = None,
                     limit: int = Query(default=60, le=520)):
        q = select(DailyMetric).order_by(DailyMetric.date.desc()).limit(limit)
        if start:
            q = q.where(DailyMetric.date >= start)
        if end:
            q = q.where(DailyMetric.date <= end)
        return db.execute(q).scalars().all()

    @r.get("/metrics/{d}", response_model=DailyMetricOut)
    def get_metric(d: date, db: Session = Depends(get_db)):
        from fastapi import HTTPException
        row = db.get(DailyMetric, d)
        if row is None:
            raise HTTPException(404, f"{d} 无体检记录")
        return row

    return r
