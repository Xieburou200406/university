# -*- coding: utf-8 -*-
"""M7-④ 持仓收盘估值快照（§RiskService.snapshot 落地）：管线落快照 →
vega/delta 限额吃当日口径；append-only 下当日行重跑覆盖、不可解析持仓 fail-soft。"""
from datetime import date

import pytest
from sqlalchemy import select

from api.services import compute_and_store_daily, latest_exposure, snapshot_positions


def _memory_session():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from db.session import make_session
    from db.models import Base
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    SM, _ = make_session(eng)
    return SM()


def _fake_panel():
    import pandas as pd
    days = pd.date_range("2026-10-12", periods=4).strftime("%Y-%m-%d").tolist()
    return pd.DataFrame({"date": days, "spot": [2.90, 2.91, 2.89, 2.92],
                         "iv_near": [0.11, 0.112, 0.109, 0.115],
                         "iv_next": [0.12] * 4,
                         "skew25": [0.01, 0.012, 0.011, 0.013],
                         "rvol20": [0.2] * 4})


def _open_position(s, code, qty=-2):
    from db.models import Position
    p = Position(code=code, qty=qty, open_date=date(2026, 10, 10),
                 open_px=0.09, status="OPEN", note=None)
    s.add(p)
    s.commit()
    return p


def test_snapshot_positions_priced_and_scaled():
    """可解析持仓 → 快照量纲 = 单张 greeks × qty × 10000；空头 vega < 0。"""
    from db.models import PositionSnapshot
    from vol.pricing.bs import bs_price, greeks
    from vol.sim.broker import R_FREE
    s = _memory_session()
    p = _open_position(s, "510050C2612M02950", qty=-2)
    d, spot, iv = date(2026, 10, 19), 2.92, 0.115
    out = snapshot_positions(s, d, spot, iv)
    assert out["snapshotted"] == 1 and out["skipped"] == []
    snap = s.execute(select(PositionSnapshot).where(
        PositionSnapshot.position_id == p.id)).scalar_one()
    T = (date(2026, 12, 23) - d).days / 365.0
    px = bs_price(spot, 2.95, T, R_FREE, iv, "C")
    dd, _, vv, tt = greeks(spot, 2.95, T, R_FREE, iv, "C")
    assert snap.mv == pytest.approx(-2 * px * 10000)
    assert snap.vega == pytest.approx(-2 * vv * 10000) and snap.vega < 0   # 空头
    assert snap.delta == pytest.approx(-2 * dd * 10000)
    assert snap.theta == pytest.approx(-2 * tt * 10000)                    # 年化口径
    assert snap.iv == iv and snap.spot == spot
    # latest_exposure 吃到快照：vega/delta 与快照行一致
    sd, v, dl = latest_exposure(s)
    assert sd == d and v == pytest.approx(snap.vega) and dl == pytest.approx(snap.delta)


def test_snapshot_positions_idempotent_same_day():
    """同日重跑 → DELETE+INSERT 覆盖（append-only 只拦 UPDATE，行数不变）。"""
    from db.models import PositionSnapshot
    s = _memory_session()
    _open_position(s, "510050P2612M02900")
    d = date(2026, 10, 19)
    snapshot_positions(s, d, 2.92, 0.115)
    snapshot_positions(s, d, 2.93, 0.116)          # 重跑（截面微调）
    rows = s.execute(select(PositionSnapshot)).scalars().all()
    assert len(rows) == 1 and rows[0].spot == 2.93


def test_snapshot_positions_unparseable_skipped():
    """遗留描述性 code → fail-soft 跳过，不阻断、不落快照。"""
    s = _memory_session()
    _open_position(s, "STRADDLE K=3.0 2026-10-28")
    out = snapshot_positions(s, date(2026, 10, 19), 2.92, 0.115)
    assert out["snapshotted"] == 0 and len(out["skipped"]) == 1
    assert "不可解析" in out["skipped"][0]["reason"]


def test_pipeline_writes_snapshots_end_to_end():
    """管线挂载：compute_and_store_daily 跑完，OPEN 持仓有当日快照。"""
    from db.models import Position, PositionSnapshot
    s = _memory_session()
    _open_position(s, "510050C2612M02950", qty=1)
    summary = compute_and_store_daily(s, __import__("api.services", fromlist=["Settings"]).Settings(),
                                      fetch_df=_fake_panel())
    assert summary["snapshot"]["snapshotted"] == 1
    snaps = s.execute(select(PositionSnapshot)).scalars().all()
    assert len(snaps) == 1 and snaps[0].date == date(2026, 10, 15)
    assert snaps[0].position_id == 1 and snaps[0].delta > 0        # 多头 Call delta > 0
    # 清理持仓后 latest_exposure 归零（OPEN 口径）
    p = s.execute(select(Position)).scalar_one()
    p.status = "CLOSED"
    s.commit()
    assert latest_exposure(s) == (date(2026, 10, 15), 0.0, 0.0)
