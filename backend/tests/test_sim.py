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
    assert set(REGISTRY) == {"sell_straddle", "buy_straddle", "sell_strangle",
                             "buy_strangle", "bull_call_spread", "bear_put_spread",
                             "calendar_spread", "iron_condor"}


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


# ---------- SSE 流式回放（§12.4 run_iter / /api/sim/stream） ----------
def test_replay_iter_matches_run():
    """run_iter 与 run 同内核：逐日消费与批量聚合结果一致。"""
    svc1, svc2 = SimAccountService(), SimAccountService()
    a1, a2 = svc1.create("批量", 10000.0), svc2.create("流式", 10000.0)
    iv, spot = [0.20] * 10 + [0.29] * 10, [3.0] * 20
    expiry = DAYS[-1] + timedelta(days=120)
    mk = lambda svc, aid: SimOrder(account_id=aid,
                                   legs=[SimLeg("C", 3.0, "buy", 1), SimLeg("P", 3.0, "buy", 1)],
                                   submit_date=DAYS[0], expiry=expiry)
    res1 = ReplaySession(svc1).run(_panel(iv, spot), [mk(svc1, a1.id)], DAYS[0], DAYS[-1])
    snaps = list(ReplaySession(svc2).run_iter(_panel(iv, spot), [mk(svc2, a2.id)], DAYS[0], DAYS[-1]))
    assert len(snaps) == 20                        # 每虚拟日一条快照
    assert all("nav" in s and "fills" in s and "events" in s for s in snaps)
    res2 = snaps[-1]                               # 最后一条是 StopIteration 前的最后快照
    # 逐日快照的末日 nav 与批量结果一致
    assert res1.nav[a1.id][-1][1] == pytest.approx(snaps[-1]["nav"][a2.id], rel=1e-6)
    assert res1.days == 20
    # 事件一致性
    assert any(e["kind"] == "iv_jump" for e in snaps[10]["events"])


def test_api_sim_stream(client):
    from db.models import SimOrder as SimOrderDB
    from sqlalchemy import select
    client.post("/api/sim/accounts", json={"name": "流式", "cash0": 50000.0})
    client.post("/api/sim/orders", json={
        "account_id": 1, "submit_date": str(DAYS[1]), "expiry": str(DAYS[-1]),
        "tpl_id": "buy_straddle", "qty": 1})
    r = client.get("/api/sim/stream", params={"from_date": str(DAYS[0]), "to_date": str(DAYS[5])})
    assert r.status_code == 200
    assert "text/event-stream" in r.headers["content-type"]
    body = r.text
    assert body.count("event: day") == 6           # 6 个虚拟日逐日推送
    assert "event: done" in body
    assert "event: error" not in body
    # 持久化已发生：订单 filled，NAV 6 个点
    import api.deps as deps
    s = deps.init_engine()()
    assert s.execute(select(SimOrderDB)).scalars().one().status == "filled"
    s.close()
    r = client.get("/api/sim/accounts/1/nav")
    assert r.status_code == 200 and len(r.json()) == 6


def test_api_sim_stream_fail_closed(client):
    r = client.get("/api/sim/stream", params={"from_date": str(DAYS[0]), "to_date": str(DAYS[0])})
    assert r.status_code == 200                    # SSE 层恒 200，错误走 error 事件
    assert "event: error" in r.text and "fail-closed" in r.text


# ---------- 错单本（§12.4 游戏化复盘） ----------
def _seed_journal_world(s, iv_path):
    """4 日截面（IV 路径给定，spot 平走）+ 信号 + 买跨式订单。返回 (sig, d0, days)。"""
    from db.models import DailyMetric, Signal
    d0 = date.today()
    days = [d0 + timedelta(days=i) for i in range(4)]
    for i, dt in enumerate(days):
        s.add(DailyMetric(date=dt, spot=2.9, atm_iv=iv_path[i], iv_pct_40=0.5))
    s.commit()
    sig = Signal(date=d0, engine="percentile", signal="LONG_VOL", confidence=0.9,
                 valid_until=d0 + timedelta(days=1))
    s.add(sig)
    s.commit()
    return sig, d0, days


def test_journal_catches_iv_crush_loss():
    """IV 衰减 + 标的横盘 → 买跨式浮亏，应被错单本收录且三段齐全。"""
    from api.services import mis_trade_journal, run_sim_replay
    from api.services import create_sim_account, create_sim_order
    s = _memory_session()
    sig, d0, days = _seed_journal_world(s, [0.20, 0.19, 0.17, 0.15])
    a = create_sim_account(s, "自由", 100000.0)
    o = create_sim_order(s, a.id, d0, d0 + timedelta(days=120), tpl_id="buy_straddle", qty=1)
    res = run_sim_replay(s, d0, days[-1])
    assert res["fills"]                                        # 已成交
    j = mis_trade_journal(s, d0, days[-1], threshold=-50.0)
    assert len(j) == 1
    e = j[0]
    assert e["order_id"] == o.id and e["pnl"] <= -50.0
    assert "LONG_VOL" in e["signal"]                           # ① 当时信号
    assert "buy" in str(e["legs"]).lower() or e["legs"][0]["side"] == "buy"   # ② 你做了什么
    assert "20.00%" in e["iv_story"] and "15.00%" in e["iv_story"]            # ③ IV 走势


def test_journal_skips_profitable_orders():
    """IV 飙升 → 买跨式盈利，错单本为空。"""
    from api.services import mis_trade_journal, run_sim_replay
    from api.services import create_sim_account, create_sim_order
    s = _memory_session()
    _, d0, days = _seed_journal_world(s, [0.20, 0.21, 0.26, 0.29])
    a = create_sim_account(s, "自由", 100000.0)
    create_sim_order(s, a.id, d0, d0 + timedelta(days=120), tpl_id="buy_straddle", qty=1)
    run_sim_replay(s, d0, days[-1])
    assert mis_trade_journal(s, d0, days[-1]) == []


def test_api_sim_journal(client):
    from datetime import date as _d
    client.post("/api/sim/accounts", json={"name": "错单", "cash0": 50000.0})
    client.post("/api/sim/orders", json={
        "account_id": 1, "submit_date": str(DAYS[1]), "expiry": str(DAYS[-1]),
        "tpl_id": "buy_straddle", "qty": 1})
    client.post("/api/sim/replay", json={"from_date": str(DAYS[0]), "to_date": str(DAYS[5])})
    r = client.get("/api/sim/journal", params={
        "from_date": str(DAYS[0]), "to_date": str(DAYS[5]), "threshold": 1e9})
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1                                      # 阈值极低 → 全部收录
    assert "signal" in body[0] and "iv_story" in body[0] and "legs" in body[0]


# ---------- 对比报告 + 归因瀑布（M6 尾巴） ----------
def test_sim_compare_and_waterfall_identity():
    """compare 出三账户指标；瀑布恒等式：起始资金 + Σ订单pnl = 期末净值（分毫不差）。"""
    from api.services import (create_sim_account, create_sim_order,
                              run_sim_replay, sim_compare, sim_waterfall)
    s = _memory_session()
    sig, d0, days = _seed_journal_world(s, [0.20, 0.21, 0.26, 0.29])
    a1 = create_sim_account(s, "跟随建议", 100000.0, auto_mirror=True)
    a2 = create_sim_account(s, "自由", 100000.0)
    create_sim_order(s, a2.id, d0, d0 + timedelta(days=120), tpl_id="buy_straddle", qty=2)
    run_sim_replay(s, d0, days[-1])

    rep = sim_compare(s, d0, days[-1])
    names = {r["name"] for r in rep["accounts"]}
    assert names == {"跟随建议", "自由"}
    for r in rep["accounts"]:
        assert r["days"] == 4 and r["final_nav"] > 0
        assert r["return"] == pytest.approx(r["final_nav"] - r["cash0"], abs=0.01)
    assert rep["benchmark"]["return_pct"] == 0.0               # spot 平走 → 基准 0%

    wf = sim_waterfall(s, a2.id, d0, days[-1])
    assert wf["identity_ok"] is True                            # 恒等式成立
    assert wf["steps"][0]["label"] == "起始资金"
    assert wf["steps"][-1]["label"] == "期末净值"
    _orders = [st for st in wf["steps"] if st["kind"] == "order"]
    assert len(_orders) == 1 and _orders[0]["delta"] == pytest.approx(
        wf["steps"][-1]["delta"] - wf["steps"][0]["delta"], abs=0.05)


def test_api_sim_compare_waterfall(client):
    client.post("/api/sim/accounts", json={"name": "对比", "cash0": 60000.0})
    client.post("/api/sim/orders", json={
        "account_id": 1, "submit_date": str(DAYS[1]), "expiry": str(DAYS[-1]),
        "tpl_id": "buy_straddle", "qty": 1})
    client.post("/api/sim/replay", json={"from_date": str(DAYS[0]), "to_date": str(DAYS[5])})
    r = client.get("/api/sim/compare", params={"from_date": str(DAYS[0]), "to_date": str(DAYS[5])})
    assert r.status_code == 200
    body = r.json()
    assert body["accounts"][0]["name"] == "对比" and len(body["benchmark"]["curve"]) == 6
    r = client.get("/api/sim/accounts/1/waterfall",
                   params={"from_date": str(DAYS[0]), "to_date": str(DAYS[5])})
    assert r.status_code == 200 and r.json()["identity_ok"] is True
    r = client.get("/api/sim/accounts/99/waterfall",
                   params={"from_date": str(DAYS[0]), "to_date": str(DAYS[5])})
    assert r.status_code == 404


# ---------- P3 模板扩容：8 个内置模板 + 腿级到期日 ----------
def test_templates_expanded_registry():
    from vol.sim.templates import prev_monthly_expiry
    assert set(REGISTRY) == {"sell_straddle", "buy_straddle", "sell_strangle", "buy_strangle",
                             "bull_call_spread", "bear_put_spread", "calendar_spread",
                             "iron_condor"}
    K = round_strike(3.0)
    # 宽跨买：两翼虚值 buy
    legs = get_template("buy_strangle").generate_legs(3.0, DAYS[-1])
    assert legs[0].strike > 3.0 > legs[1].strike and all(l.side == "buy" for l in legs)
    # 牛市价差：买低卖高 Call
    legs = get_template("bull_call_spread").generate_legs(3.0, DAYS[-1])
    assert legs[0].side == "buy" and legs[1].side == "sell" and legs[1].strike > legs[0].strike
    # 熊市价差：买高卖低 Put
    legs = get_template("bear_put_spread").generate_legs(3.0, DAYS[-1])
    assert legs[0].side == "buy" and legs[1].side == "sell" and legs[1].strike < legs[0].strike
    # 日历价差：同 K，卖近月买远月（腿级到期）
    far = date(2026, 12, 23)
    legs = get_template("calendar_spread").generate_legs(3.0, far)
    assert legs[0].expiry < legs[1].expiry == far
    assert legs[0].side == "sell" and legs[1].side == "buy"
    assert legs[0].strike == legs[1].strike
    assert prev_monthly_expiry(far) == date(2026, 11, 25)      # 近月=前一月第四个周三
    # 铁鹰：4 腿，卖内翼买外翼
    legs = get_template("iron_condor").generate_legs(3.0, DAYS[-1])
    assert len(legs) == 4
    assert [l.side for l in legs] == ["sell", "buy", "sell", "buy"]
    assert legs[1].strike > legs[0].strike and legs[3].strike < legs[2].strike


def test_calendar_spread_per_leg_expiry_replay():
    """日历价差端到端：腿级到期持久化 → 撮合（近月到期前）→ 估值按各腿自己的 T。"""
    from api.services import create_sim_account, create_sim_order, run_sim_replay
    from db.models import SimFill as SimFillDB, SimOrder as SimOrderDB
    from sqlalchemy import select
    s = _memory_session()
    sig, d0, days = _seed_journal_world(s, [0.20, 0.20, 0.20, 0.20])
    a = create_sim_account(s, "自由", 100000.0)
    far = d0 + timedelta(days=150)
    o = create_sim_order(s, a.id, d0, far, tpl_id="calendar_spread", qty=1)
    # 持久化的 legs_json 带腿级到期
    o_db = s.execute(select(SimOrderDB)).scalar_one()
    assert o_db.legs_json[0]["expiry"] is not None
    assert o_db.legs_json[0]["expiry"] < str(far)
    res = run_sim_replay(s, d0, days[-1])
    assert len(res["fills"]) == 2 and res["days"] == 4          # 两腿同日成交
    fills = s.execute(select(SimFillDB).order_by(SimFillDB.leg_index)).scalars().all()
    assert fills[0].fill_px < fills[1].fill_px                  # 近月权利金 < 远月（同 K 同 IV）


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


# ---------- M6-② 自动镜像（跟随建议账户） ----------
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


def _seed_mirror_world(s, n_days=4, signal="LONG_VOL"):
    """今日起 n 个连续日的截面 + auto_mirror 账户 + 信号。返回 (sig, d0, days)。"""
    from db.models import DailyMetric, Signal
    from db.models import SimAccount as SimAccountDB
    d0 = date.today()
    days = [d0 + timedelta(days=i) for i in range(n_days)]
    for i, dt in enumerate(days):
        s.add(DailyMetric(date=dt, spot=2.9 + 0.005 * i, atm_iv=0.20 + 0.001 * i,
                          iv_pct_40=0.5))
    s.add(SimAccountDB(name="跟随建议", auto_mirror=True, cash0=100000.0))
    s.commit()
    sig = Signal(date=d0, engine="percentile", signal=signal, confidence=0.9,
                 valid_until=d0 + timedelta(days=1), detail_json={"iv_pct": 0.8})
    s.add(sig)
    s.commit()
    return sig, d0, days


def test_mirror_expiry_picks_monthly_wednesday():
    from vol.sim.mirror import mirror_expiry
    # 2026-10-16 + 14 天 = 10-30 之后最近的第四个星期三 → 2026-11-25
    assert mirror_expiry(date(2026, 10, 16)) == date(2026, 11, 25)
    assert mirror_expiry(date(2026, 10, 16)).weekday() == 2     # 周三


def test_mirror_long_vol_creates_buy_straddle_and_dedupes():
    from api.services import generate_advice
    from vol.config import Settings
    from vol.sim.mirror import mirror_advice
    s = _memory_session()
    sig, d0, _ = _seed_mirror_world(s, signal="LONG_VOL")
    card = generate_advice(s, Settings(), sig.id)
    assert card.verdict_json["verdict"] == "PASS"
    orders = s.execute(__import__("sqlalchemy", fromlist=["select"]).select(
        __import__("db.models", fromlist=["SimOrder"]).SimOrder)).scalars().all()
    assert len(orders) == 1
    o = orders[0]
    assert o.tpl_id == "auto:long_vol" and o.status == "pending"
    assert o.submit_date == d0
    assert (o.params_json or {}).get("signal_id") == sig.id
    legs = o.legs_json
    assert {l["cp"] for l in legs} == {"C", "P"} and all(l["side"] == "buy" for l in legs)
    assert all(l["strike"] == round_strike(2.9) for l in legs)   # ATM
    assert 1 <= legs[0]["qty"] <= 20
    # 卡面写回镜像信息
    assert card.card_json["auto_mirror"]["orders"][0]["order_id"] == o.id
    # 去重：重复生成同一信号的建议 → 不重复建仓
    card2 = generate_advice(s, Settings(), sig.id)
    orders2 = s.execute(__import__("sqlalchemy", fromlist=["select"]).select(
        __import__("db.models", fromlist=["SimOrder"]).SimOrder)).scalars().all()
    assert len(orders2) == 1 and card2.card_json.get("auto_mirror") is None


def test_mirror_short_vol_creates_sell_strangle():
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig, _, _ = _seed_mirror_world(s, signal="SHORT_VOL")
    card = generate_advice(s, Settings(), sig.id)
    from sqlalchemy import select
    from db.models import SimOrder as SimOrderDB
    o = s.execute(select(SimOrderDB)).scalar_one()
    assert o.tpl_id == "auto:short_vol"
    legs = o.legs_json
    assert {l["cp"] for l in legs} == {"C", "P"} and all(l["side"] == "sell" for l in legs)
    assert legs[0]["strike"] > 2.9 > legs[1]["strike"]           # 虚值两翼
    assert card.verdict_json["verdict"] == "PASS"


def test_mirror_skips_reject_and_neutral():
    from api.services import generate_advice
    from vol.config import Settings
    from sqlalchemy import select
    from db.models import SimOrder as SimOrderDB
    # REJECT：valid_until 已过 → 建议作废，不镜像
    s = _memory_session()
    sig, _, _ = _seed_mirror_world(s, signal="LONG_VOL")
    sig.valid_until = date.today() - timedelta(days=1)
    sig.date = date.today() - timedelta(days=2)
    s.commit()
    card = generate_advice(s, Settings(), sig.id)
    assert card.verdict_json["verdict"] == "REJECT"
    assert s.execute(select(SimOrderDB)).scalars().all() == []
    # NEUTRAL：观望也是建议，但不建仓
    s2 = _memory_session()
    sig2, _, _ = _seed_mirror_world(s2, signal="NEUTRAL")
    card2 = generate_advice(s2, Settings(), sig2.id)
    assert card2.verdict_json["verdict"] == "PASS"
    assert s2.execute(select(SimOrderDB)).scalars().all() == []


def test_mirror_no_mirror_account_is_noop():
    from api.services import generate_advice
    from vol.config import Settings
    from sqlalchemy import select
    from db.models import DailyMetric, Signal
    from db.models import SimAccount as SimAccountDB
    from db.models import SimOrder as SimOrderDB
    s = _memory_session()
    d0 = date.today()
    s.add(DailyMetric(date=d0, spot=2.9, atm_iv=0.2, iv_pct_40=0.5))
    s.add(SimAccountDB(name="自由账户", auto_mirror=False, cash0=50000.0))   # 非镜像账户
    s.commit()
    sig = Signal(date=d0, engine="percentile", signal="LONG_VOL", confidence=0.9,
                 valid_until=d0 + timedelta(days=1))
    s.add(sig)
    s.commit()
    generate_advice(s, Settings(), sig.id)
    assert s.execute(select(SimOrderDB)).scalars().all() == []


def test_mirror_order_fills_in_replay():
    """端到端：建议卡镜像 → 回放撮合成交 → 净值打点。"""
    from api.services import generate_advice, run_sim_replay
    from vol.config import Settings
    s = _memory_session()
    sig, d0, days = _seed_mirror_world(s, n_days=4, signal="LONG_VOL")
    generate_advice(s, Settings(), sig.id)
    res = run_sim_replay(s, d0, days[-1])
    assert res["days"] == 4
    assert res["fills"], "镜像单应在首个虚拟日成交"
    assert all(f["side"] == "buy" for f in res["fills"])
    assert len(res["fills"]) == 2                                # 跨式两腿
    from sqlalchemy import select
    from db.models import SimNav as SimNavDB
    navs = s.execute(select(SimNavDB)).scalars().all()
    assert len(navs) == 4                                        # 逐日打点
