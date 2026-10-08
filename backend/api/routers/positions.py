# -*- coding: utf-8 -*-
"""/api/positions —— 模拟持仓账本（记账用，非真实下单）。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db
from api.schemas import PositionClose, PositionCreate, PositionOut, PositionSnapshotOut
from db.models import Position, PositionSnapshot


def router() -> APIRouter:
    r = APIRouter()

    @r.get("/positions", response_model=list[PositionOut])
    def list_positions(db: Session = Depends(get_db), status: str | None = None):
        q = select(Position).order_by(Position.id.desc())
        if status:
            q = q.where(Position.status == status)
        return db.execute(q).scalars().all()

    @r.post("/positions", response_model=PositionOut, status_code=201)
    def create(body: PositionCreate, db: Session = Depends(get_db)):
        pos = Position(code=body.code, qty=body.qty, open_date=body.open_date,
                       open_px=body.open_px, note=body.note, status="open")
        db.add(pos)
        db.flush()
        return pos

    @r.post("/positions/{pid}/close", response_model=PositionOut)
    def close(pid: int, body: PositionClose, db: Session = Depends(get_db)):
        pos = db.get(Position, pid)
        if pos is None:
            raise HTTPException(404, f"position {pid} 不存在")
        if pos.status != "open":
            raise HTTPException(409, f"position {pid} 已平仓（{pos.close_date}）")
        if body.close_date < pos.open_date:
            raise HTTPException(422, "平仓日早于开仓日")
        pos.close_date, pos.close_px, pos.status = body.close_date, body.close_px, "closed"
        db.flush()
        return pos

    @r.get("/positions/{pid}/snapshots", response_model=list[PositionSnapshotOut])
    def snapshots(pid: int, db: Session = Depends(get_db),
                  start: date | None = None, end: date | None = None,
                  limit: int = Query(default=60, le=520)):
        if db.get(Position, pid) is None:
            raise HTTPException(404, f"position {pid} 不存在")
        q = (select(PositionSnapshot).where(PositionSnapshot.position_id == pid)
             .order_by(PositionSnapshot.date.desc()).limit(limit))
        if start:
            q = q.where(PositionSnapshot.date >= start)
        if end:
            q = q.where(PositionSnapshot.date <= end)
        return db.execute(q).scalars().all()

    return r
