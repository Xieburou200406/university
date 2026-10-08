# -*- coding: utf-8 -*-
"""/api/advice —— 建议卡片（审计存档）。生成即跑风控链并留 trace；只建议，绝不下单（铁律 10）。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db, get_settings
from api.schemas import AdviceCardOut, AdviceGenerateOut, RiskCheck
from api.services import generate_advice
from db.models import AdviceCard


def router() -> APIRouter:
    r = APIRouter()

    @r.post("/advice/generate/{signal_id}", response_model=AdviceGenerateOut)
    def gen(signal_id: int, db: Session = Depends(get_db),
            settings=Depends(get_settings)):
        try:
            ac = generate_advice(db, settings, signal_id)
        except LookupError as e:
            raise HTTPException(404, str(e))
        checks = [RiskCheck(**c) for c in ac.verdict_json["checks"]]
        return AdviceGenerateOut(card=AdviceCardOut.model_validate(ac),
                                 verdict=ac.verdict_json["verdict"], risk_checks=checks)

    @r.get("/advice", response_model=list[AdviceCardOut])
    def list_cards(limit: int = 20, db: Session = Depends(get_db)):
        q = select(AdviceCard).order_by(AdviceCard.id.desc()).limit(min(limit, 100))
        return db.execute(q).scalars().all()

    @r.get("/advice/latest", response_model=AdviceCardOut)
    def latest(db: Session = Depends(get_db)):
        row = db.execute(select(AdviceCard).order_by(AdviceCard.id.desc())
                         .limit(1)).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "尚无建议卡")
        return row

    return r
