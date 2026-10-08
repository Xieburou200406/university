# -*- coding: utf-8 -*-
"""/api/reviews —— 周度复核结果（只读）；/api/pipeline —— 管线运行记录 + 触发。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import PipelineRunOut, ReviewLogOut
from api.services import trigger_pipeline
from db.models import PipelineRun, ReviewLog


def reviews_router() -> APIRouter:
    r = APIRouter()

    @r.get("/reviews", response_model=list[ReviewLogOut])
    def list_reviews(limit: int = 12, db: Session = Depends(get_db)):
        return db.execute(select(ReviewLog).order_by(ReviewLog.id.desc())
                          .limit(min(limit, 52))).scalars().all()

    return r


def pipeline_router() -> APIRouter:
    r = APIRouter()

    @r.get("/pipeline/runs", response_model=list[PipelineRunOut])
    def list_runs(limit: int = 10, db: Session = Depends(get_db)):
        return db.execute(select(PipelineRun).order_by(PipelineRun.id.desc())
                          .limit(min(limit, 50))).scalars().all()

    @r.post("/pipeline/run", response_model=PipelineRunOut)
    def run(db: Session = Depends(get_db)):
        """触发每日信号管线（防重入锁）。联网失败 → fail-closed 记 failed。"""
        try:
            return trigger_pipeline(db)
        except PermissionError as e:
            raise HTTPException(409, str(e))
        except Exception as e:                      # noqa: BLE001 —— 管线失败已是常规业务态
            raise HTTPException(502, f"管线失败（fail-closed）：{e}") from e

    return r
