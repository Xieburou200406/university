# -*- coding: utf-8 -*-
"""/api/signals —— 信号流水（只读）。valid_only=true 只看未过 T+1 有效期的（铁律 8）。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import SignalDetailOut, SignalOut
from db.models import AdviceCard, Signal


def router() -> APIRouter:
    r = APIRouter()

    @r.get("/signals", response_model=list[SignalOut])
    def list_signals(db: Session = Depends(get_db),
                     engine: str | None = None, signal: str | None = None,
                     valid_only: bool = False, limit: int = Query(default=30, le=200)):
        q = select(Signal).order_by(Signal.id.desc()).limit(limit)
        if engine:
            q = q.where(Signal.engine == engine)
        if signal:
            q = q.where(Signal.signal == signal)
        if valid_only:
            q = q.where(Signal.valid_until >= date.today())
        return db.execute(q).scalars().all()

    @r.get("/signals/{sid}", response_model=SignalDetailOut)
    def get_signal(sid: int, db: Session = Depends(get_db)):
        sig = db.get(Signal, sid)
        if sig is None:
            raise HTTPException(404, f"signal {sid} 不存在")
        out = SignalDetailOut.model_validate(sig)
        cards = db.execute(select(AdviceCard).where(AdviceCard.signal_id == sid)
                           .order_by(AdviceCard.id.desc())).scalars().all()
        out.advice_cards = cards
        return out

    return r
