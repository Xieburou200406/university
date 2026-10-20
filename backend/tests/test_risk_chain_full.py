# -*- coding: utf-8 -*-
"""M7-⑥ 风控条件链 7 条完整化：generate_advice 新增 5 条检查的逐条验证。
设计 §risk/rules.py：SingleStrike / DeepMoney / CapitalUsage / LiquidityGate /
SignalConsistency。空组合必须全 PASS 且不触网；软警告不影响 REJECT 语义。"""
from datetime import date, timedelta

import pytest

from db.models import Position, PositionSnapshot, Signal


def _memory_session():
    import api.deps as _deps  # noqa: F401
    from db.models import Base
    from db.session import make_engine, make_session
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session, _ = make_session(engine)
    return Session()


def _seed_signal(s, signal="LONG_VOL"):
    d0 = date.today()
    sig = Signal(date=d0, engine="percentile", signal=signal, confidence=0.9,
                 valid_until=d0 + timedelta(days=1), detail_json={"iv_pct": 0.8})
    s.add(sig)
    s.commit()
    return sig


def _pos(s, code, qty, open_px=0.06):
    p = Position(code=code, qty=qty, open_date=date.today(), open_px=open_px,
                 status="open")
    s.add(p)
    s.commit()
    return p


def _snap(s, pid, d, delta, mv=600.0, vega=10.0, spot=2.9):
    row = PositionSnapshot(date=d, position_id=pid, mv=mv, delta=delta,
                           vega=vega, theta=-1.0, iv=0.2, spot=spot)
    s.add(row)
    s.commit()
    return row


CHECK_NAMES = {"signal_valid_until", "vega_limit", "delta_limit", "floating_loss",
               "price_limit", "single_strike", "deep_money", "capital_usage",
               "liquidity_gate", "signal_consistency"}


def _check(card, name):
    return next(c for c in card.verdict_json["checks"] if c["name"] == name)


# ---------- 空组合：全 PASS 且不触网 ----------

def test_empty_portfolio_all_new_checks_pass():
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s)
    card = generate_advice(s, Settings(), sig.id)
    names = {c["name"] for c in card.verdict_json["checks"]}
    assert CHECK_NAMES <= names, f"缺检查项: {CHECK_NAMES - names}"
    assert card.verdict_json["verdict"] == "PASS"
    assert all(c["passed"] for c in card.verdict_json["checks"])


# ---------- ⑥ SingleStrikeRule：同一行权价集中度 ----------

def test_single_strike_exceeds_limit_warns():
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s)
    code = "510050C2611M02900"                      # strike 2.9
    for _ in range(3):
        _pos(s, code, 10)                           # 同档 30 张 > 上限 20
    card = generate_advice(s, Settings(single_strike_limit=20), sig.id)
    c = _check(card, "single_strike")
    assert not c["passed"] and "2.900" in c["detail"] and "30" in c["detail"]
    assert card.verdict_json["verdict"] == "WARN"   # 软警告不 REJECT
    assert "集中度超限" in card.card_json["action"]


def test_single_strike_unparseable_code_skipped():
    """代码不可解析 fail-soft 跳过（不虚构行权价），其余持仓照常聚合并留痕。"""
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s)
    _pos(s, "遗留演示持仓-STRADDLE", 5)              # 不可解析
    _pos(s, "510050C2611M02900", 2)
    card = generate_advice(s, Settings(single_strike_limit=20), sig.id)
    c = _check(card, "single_strike")
    assert c["passed"] and "1 个持仓代码不可解析" in c["detail"]


# ---------- ⑦ DeepMoneyRule：单张 |Δ| 越界禁加仓 ----------

def test_deep_money_flags_extreme_delta():
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s)
    p = _pos(s, "510050C2611M02900", 1)
    d0 = date.today()
    _snap(s, p.id, d0, delta=0.9 * 10000)           # 单张 Δ=0.90 > 0.85
    card = generate_advice(s, Settings(), sig.id)
    c = _check(card, "deep_money")
    assert not c["passed"] and "Δ=0.90" in c["detail"]
    assert card.verdict_json["verdict"] == "WARN"


def test_deep_money_passes_when_delta_in_band():
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s)
    p = _pos(s, "510050C2611M02900", 1)
    _snap(s, p.id, date.today(), delta=0.50 * 10000)
    card = generate_advice(s, Settings(), sig.id)
    assert _check(card, "deep_money")["passed"]


def test_deep_money_no_snapshot_skips():
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s)
    _pos(s, "510050C2611M02900", 1)                 # 有持仓、无快照
    card = generate_advice(s, Settings(), sig.id)
    c = _check(card, "deep_money")
    assert c["passed"] and "跳过" in c["detail"]    # 不虚构估值


# ---------- ⑧ CapitalUsageRule：保证金占用 / 基准资金 ----------

def test_capital_usage_exceeds_limit():
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s)
    p = _pos(s, "510050P2611M02900", -5)            # 空头 → 保证金估算非零
    d0 = date.today()
    _snap(s, p.id, d0, delta=-0.3 * -5 * 10000, mv=-5 * 0.06 * 10000)
    card = generate_advice(s, Settings(capital_base=100.0), sig.id)  # 分母极小 → 超限
    c = _check(card, "capital_usage")
    assert not c["passed"] and "上限 80%" in c["detail"]
    assert card.verdict_json["verdict"] == "WARN"


def test_capital_usage_passes_normal():
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s)
    card = generate_advice(s, Settings(), sig.id)   # 无持仓 → 占用 0%
    c = _check(card, "capital_usage")
    assert c["passed"] and "0.0%" in c["detail"]


# ---------- ⑨ LiquidityGateRule：盘口分 < 阈值 → 软警告；断网 fail-open ----------

def test_liquidity_gate_low_score_warns(monkeypatch):
    import api.services as services
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s)
    _pos(s, "510050C2611M02900", 1)
    monkeypatch.setattr(services, "fetch_live_quotes",
                        lambda codes: {"510050C2611M02900": {
                            "bid": 0.0500, "ask": 0.0800,          # 价差率 ~46% → 价差部分≈0
                            "volume": 0.0, "oi": 0.0}})
    # 价差部分 0 分 + 单合约量分位各 1.0 → 恰 40 分；阈值 45 才触发
    card = generate_advice(s, Settings(liq_min_score=45), sig.id)
    c = _check(card, "liquidity_gate")
    assert not c["passed"] and "低于 45 分" in c["detail"]
    assert card.verdict_json["verdict"] == "WARN"


def test_liquidity_gate_network_down_fail_open(monkeypatch):
    import api.services as services
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s)
    _pos(s, "510050C2611M02900", 1)
    monkeypatch.setattr(services, "fetch_live_quotes", lambda codes: {})
    card = generate_advice(s, Settings(), sig.id)
    c = _check(card, "liquidity_gate")
    assert c["passed"] and "fail-open" in c["detail"]
    assert card.verdict_json["verdict"] == "PASS"


# ---------- ⑩ SignalConsistencyRule：方向同源留痕 ----------

def test_signal_consistency_records_source():
    from api.services import generate_advice
    from vol.config import Settings
    s = _memory_session()
    sig = _seed_signal(s, signal="SHORT_VOL")
    card = generate_advice(s, Settings(), sig.id)
    c = _check(card, "signal_consistency")
    assert c["passed"]
    assert f"signal#{sig.id}" in c["detail"] and "SHORT_VOL" in c["detail"]
