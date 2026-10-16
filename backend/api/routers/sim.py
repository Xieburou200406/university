# -*- coding: utf-8 -*-
"""/api/sim —— 模拟盘（§12.4，M6-①）。虚拟资金练手场，与真实账户物理隔离（铁律 10）。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.deps import get_db
from api.services import create_sim_account, create_sim_order, run_sim_replay
from db.models import SimAccount, SimFill, SimNav, SimOrder


class SimAccountIn(BaseModel):
    name: str
    cash0: float = 100000.0
    auto_mirror: bool = False


class SimOrderIn(BaseModel):
    account_id: int
    submit_date: date
    expiry: date
    tpl_id: str | None = None
    params: dict | None = None
    legs: list[dict] | None = None
    qty: int = 1
    price_type: str = "market"
    limit_px: float | None = None


class SimReplayIn(BaseModel):
    from_date: date
    to_date: date
    account_ids: list[int] | None = None


def router() -> APIRouter:
    r = APIRouter()

    @r.post("/sim/accounts")
    def add_account(body: SimAccountIn, db: Session = Depends(get_db)):
        a = create_sim_account(db, body.name, body.cash0, body.auto_mirror)
        return {"id": a.id, "name": a.name, "cash0": a.cash0, "auto_mirror": a.auto_mirror}

    @r.get("/sim/accounts")
    def list_accounts(db: Session = Depends(get_db)):
        rows = db.execute(select(SimAccount).order_by(SimAccount.id)).scalars().all()
        return [{"id": a.id, "name": a.name, "cash0": a.cash0,
                 "auto_mirror": a.auto_mirror, "status": a.status} for a in rows]

    @r.post("/sim/orders")
    def add_order(body: SimOrderIn, db: Session = Depends(get_db)):
        try:
            o = create_sim_order(db, body.account_id, body.submit_date, body.expiry,
                                 tpl_id=body.tpl_id, params=body.params, legs=body.legs,
                                 qty=body.qty, price_type=body.price_type,
                                 limit_px=body.limit_px)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(422, str(e))
        return {"id": o.id, "status": o.status, "legs": o.legs_json}

    @r.post("/sim/replay")
    def replay(body: SimReplayIn, db: Session = Depends(get_db)):
        try:
            return run_sim_replay(db, body.from_date, body.to_date, body.account_ids)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))

    @r.get("/sim/accounts/{aid}/nav")
    def nav(aid: int, db: Session = Depends(get_db)):
        rows = db.execute(select(SimNav).where(SimNav.account_id == aid)
                          .order_by(SimNav.ts)).scalars().all()
        if not rows:
            raise HTTPException(404, "该账户尚无净值（先 POST /api/sim/replay）")
        return [{"ts": str(x.ts), "nav": x.nav} for x in rows]

    @r.get("/sim/fills")
    def fills(limit: int = 50, db: Session = Depends(get_db)):
        rows = db.execute(select(SimFill).order_by(SimFill.id.desc())
                          .limit(min(limit, 200))).scalars().all()
        return [{"id": x.id, "order_id": x.order_id, "date": str(x.filled_date),
                 "cp": x.cp, "strike": x.strike, "side": x.side, "qty": x.qty,
                 "fill_px": x.fill_px, "cost": x.cost} for x in rows]

    return r
