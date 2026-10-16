# -*- coding: utf-8 -*-
"""M6-① 模拟盘：时钟 / 撮合 / 账户 / 模板 / 回放集成 / API 端到端。"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vol.sim import (HALF_SPREAD, FEE_PER_UNIT, ReplayClock, ReplaySession,
                     SimAccountService, SimLeg, SimOrder, option_px, round_strike)
from vol.sim.templates import REGISTRY, get_template

D0 = date(2026, 9, 1)
DAYS = [D0 + timedelta(days=i) for i in range(20)]        # 测试用连续日（非真实日历，无妨）


# ---------- 时钟 ----------
def test_clock_step_and_done():
    c = ReplayClock(DAYS)
    assert c.now() == D0
    for _ in range(19):
        assert c.step() is True
    assert c.now() == DAYS[-1] and c.done
    assert c.step() is False
    assert c.peek_next() is None


def test_clock_empty_fails():
    with pytest.raises(ValueError):
        ReplayClock([])


# ---------- 撮合 ----------
def _order(legs, submit=DAYS[0], expiry=None, price_type="market", limit_px=None):
    return SimOrder(account_id=1, legs=legs, submit_date=submit,
                    expiry=expiry or DAYS[-1], price_type=price_type, limit_px=limit_px)


def test_market_fill_price_and_cost():
    o = _order([SimLeg("C", 3.0, "buy", 1)])
    fills = o and __import__("vol.sim.broker", fromlist=["try_match"]).try_match(
        o, DAYS[0], spot=3.0, sigma=0.25)
    assert fills and o.status == "filled"
    px = option_px(3.0, 3.0, (DAYS[-1] - DAYS[0]).days / 365.0, 0.25, "C")
    assert fills[0].fill_px == pytest.approx(px * (1 + HALF_SPREAD), rel=1e-6)
    assert fills[0].cost == pytest.approx(FEE_PER_UNIT)


def test_limit_order_not_touch_then_cancel_at_expiry():
    br = __import__("vol.sim.broker", fromlist=["try_match"])
    o = _order([SimLeg("P", 2.8, "buy", 1)], price_type="limit", limit_px=0.0001)
    assert br.try_match(o, DAYS[0], 3.0, 0.25) is None       # 限价远低于市价 → 不成
    assert o.status == "pending"
    o2 = _order([SimLeg("P", 2.8, "buy", 1)], expiry=DAYS[5])
    assert br.try_match(o2, DAYS[9], 3.0, 0.25) is None      # 已过到期 → 作废
    assert o2.status == "cancelled"


def test_submit_date_information_wall():
    br = __import__("vol.sim.broker", fromlist=["try_match"])
    o = _order([SimLeg("C", 3.0, "buy", 1)], submit=DAYS[10])
    assert br.try_match(o, DAYS[0], 3.0, 0.25) is None       # 信息墙：未到提交日不撮合
    assert br.try_match(o, DAYS[10], 3.0, 0.25) is not None


# ---------- 账户净值 ----------
def test_sell_straddle_cash_and_nav_signs():
    br = __import__("vol.sim.broker", fromlist=["try_match"])
    svc = SimAccountService()
    a = svc.create("跟随建议", 100000.0)
    o = _order([SimLeg("C", 3.0, "sell", 1), SimLeg("P", 3.0, "sell", 1)])
    fills = br.try_match(o, DAYS[0], 3.0, 0.25)
    svc.apply_fills(a.id, fills)
    assert a.cash > 100000.0                                  # 卖方收权利金（扣费后仍为正流入）
    nav = svc.mark(a.id, DAYS[0], 3.0, 0.25)
    T = (DAYS[-1] - DAYS[0]).days / 365.0
    mid_c = option_px(3.0, 3.0, T, 0.25, "C")                 # 估值用中间价（无价差）
    mid_p = option_px(3.0, 3.0, T, 0.25, "P")
    expected = a.cash - (mid_c + mid_p) * 10000.0             # 空头负债 = 双腿中间价市值
    assert nav == pytest.approx(expected, rel=1e-6)
    curve = svc.nav_curve(a.id)
    assert curve[-1][0] == DAYS[0] and curve[-1][1] == pytest.approx(nav, abs=0.01)


def test_expiry_settles_intrinsic_value():
    br = __import__("vol.sim.broker", fromlist=["try_match"])
    svc = SimAccountService()
    a = svc.create("x", 10000.0)
    o = _order([SimLeg("C", 2.8, "buy", 1)], expiry=DAYS[5])
    fills = br.try_match(o, DAYS[0], 3.0, 0.25)
    svc.apply_fills(a.id, fills)
    svc.mark(a.id, DAYS[4], 3.0, 0.25)                        # 未到期：时间价值仍在
    cash_before = a.cash
    nav = svc.mark(a.id, DAYS[5], 2.9, 0.25)                  # 到期日 spot=2.9 → 内在 0.1
    assert a.positions == {}                                  # 到期了结
    assert nav == pytest.approx(cash_before + 0.1 * 10000.0, abs=0.5)


# ---------- 模板 ----------
def test_templates_strike_grid():
    legs = get_template("sell_straddle").generate_legs(2.934, DAYS[-1])
    assert {l.cp for l in legs} == {"C", "P"}
    assert all(l.strike == round_strike(2.934) == 2.95 for l in legs)
    legs2 = get_template("sell_strangle").generate_legs(3.0, DAYS[-1], params={"width": 0.1})
    assert legs2[0].strike == 3.30 and legs2[1].strike == 2.70
    with pytest.raises(KeyError):
        get_template("nope")
    assert set(REGISTRY) == {"sell_straddle", "buy_straddle", "sell_strangle"}


# ---------- 回放集成：IV 飙升 → 买跨式应显著跑赢基准 ----------
def _panel(iv_path, spot_path):
    return pd.DataFrame({"date": DAYS, "spot": spot_path, "iv_near": iv_path})


def test_replay_buy_straddle_wins_on_iv_spike():
    svc = SimAccountService()
    a = svc.create("自由", 10000.0)
    iv = [0.20] * 10 + [0.29] * 10                               # 第 11 日 IV 单日跳升 9 个点
    spot = [3.0] * 20
    expiry = DAYS[-1] + timedelta(days=120)                      # 远月：Vega 主导而非 Theta 衰减
    o = SimOrder(account_id=a.id, legs=[SimLeg("C", 3.0, "buy", 1), SimLeg("P", 3.0, "buy", 1)],
                 submit_date=DAYS[0], expiry=expiry)
    res = ReplaySession(svc).run(_panel(iv, spot), [o], DAYS[0], DAYS[-1])
    assert res.fills and len(res.fills) == 2                     # 首日市价成交
    navs = [v for _, v in res.nav[a.id]]
    assert navs[-1] > navs[0] + 100.0                            # IV 20%→29%：跨式多头显著盈利
    assert any(e["kind"] == "iv_jump" for e in res.events)       # 跳变事件打标
    assert res.benchmark[-1][1] == pytest.approx(10000.0)        # 标的横盘 → 基准平


# ---------- API 端到端（注入内存库） ----------
@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from db.session import make_session
    from db.models import Base
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                        poolclass=__import__("sqlalchemy.pool", fromlist=["StaticPool"]).StaticPool)
    Base.metadata.create_all(eng)
    SM, _ = make_session(eng)
    s = SM()
    # 造 6 日行情截面
    for i, dt in enumerate(DAYS[:6]):
        s.add(__import__("db.models", fromlist=["DailyMetric"]).DailyMetric(
            date=dt, spot=2.9 + 0.01 * i, atm_iv=0.2 + 0.01 * i, iv_pct_40=0.5))
    s.commit()
    import api.deps as deps
    monkeypatch.setattr(deps, "init_engine", lambda: SM)
    from api.app import create_app
    yield TestClient(create_app())
    s.close()


def test_api_sim_e2e(client):
    r = client.post("/api/sim/accounts", json={"name": "练习", "cash0": 50000.0})
    assert r.status_code == 200 and r.json()["id"] == 1
    aid = r.json()["id"]
    r = client.post("/api/sim/orders", json={
        "account_id": aid, "submit_date": str(DAYS[1]), "expiry": str(DAYS[-1]),
        "tpl_id": "buy_straddle", "qty": 1})
    assert r.status_code == 200 and len(r.json()["legs"]) == 2
    assert r.json()["legs"][0]["strike"] == round_strike(2.91)
    r = client.post("/api/sim/replay", json={
        "from_date": str(DAYS[0]), "to_date": str(DAYS[5])})
    assert r.status_code == 200 and r.json()["days"] == 6
    assert r.json()["fills"], "订单应成交"
    r = client.get(f"/api/sim/accounts/{aid}/nav")
    assert r.status_code == 200 and len(r.json()) == 6
    r = client.get("/api/sim/fills")
    assert r.status_code == 200 and r.json()
    # 幂等：重放不重复成交（订单已 filled）
    r2 = client.post("/api/sim/replay", json={
        "from_date": str(DAYS[0]), "to_date": str(DAYS[5])})
    assert r2.status_code == 200 and r2.json()["fills"] == []


def test_api_sim_info_wall_404(client):
    client.post("/api/sim/accounts", json={"name": "x"})
    r = client.post("/api/sim/orders", json={
        "account_id": 1, "submit_date": "2030-01-01", "expiry": "2030-02-01",
        "legs": [{"cp": "C", "strike": 2.9, "side": "buy", "qty": 1}]})
    assert r.status_code == 404 and "信息墙" in r.json()["detail"]
