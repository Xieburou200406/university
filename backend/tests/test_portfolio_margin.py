# -*- coding: utf-8 -*-
"""组合保证金（backlog：买腿覆盖卖腿）：同 (cp, 到期月) 内价差组合按行权价差计收，
其余维持单腿上界。铁律 1（行权价 ÷1000）贯穿全部用例。纯函数离线测试。"""
import pytest

from vol import position_risk as pr


# ---------- 覆盖成立：认购牛市价差（买低卖高） ----------

def test_bull_call_spread_charged_at_strike_diff():
    # 买 K=2.700 Call + 卖 K=2.800 Call，同到期月 → 卖腿按价差 0.100×10000=1000 元计收
    legs = [("510050C2610M02700", 1, 0.10, 2.918),
            ("510050C2610M02800", -1, 0.05, 2.918)]
    pm = pr.estimate_portfolio_margin(legs)
    full = pr.estimate_margin_short(0.05, 2.918)
    assert pm["total"] == pytest.approx(1000.0, rel=1e-6)
    assert pm["standalone_total"] == pytest.approx(full, rel=1e-6)
    assert pm["relief"] == pytest.approx(full - 1000.0, rel=1e-6)
    assert pm["covered_contracts"] == 1
    assert pm["pairs"][0]["covered_by"] == 2.7


def test_equal_strike_long_short_zero_margin():
    # 同行权价买卖对敲 → 完美对冲，保证金 0
    legs = [("510050C2610M02700", 1, 0.10, 2.918),
            ("510050C2610M02700", -1, 0.10, 2.918)]
    pm = pr.estimate_portfolio_margin(legs)
    assert pm["total"] == 0.0
    assert pm["covered_contracts"] == 1


# ---------- 覆盖成立：认沽熊市价差（买高卖低） ----------

def test_bear_put_spread_charged_at_strike_diff():
    # 买 K=2.900 Put + 卖 K=2.800 Put → 价差 0.100×10000
    legs = [("510050P2610M02900", 1, 0.12, 2.918),
            ("510050P2610M02800", -1, 0.06, 2.918)]
    pm = pr.estimate_portfolio_margin(legs)
    assert pm["total"] == pytest.approx(1000.0, rel=1e-6)


def test_uncovered_direction_no_relief():
    # 反向价差：Call 买高卖低（认购熊市价差，损失=权利金收入无封顶需求…但保守起见不抵扣）
    # 买 K=2.800 覆盖不了卖 K=2.700（Call 要求买腿行权价 ≤ 卖腿）
    legs = [("510050C2610M02800", 1, 0.10, 2.918),
            ("510050C2610M02700", -1, 0.05, 2.918)]
    pm = pr.estimate_portfolio_margin(legs)
    assert pm["relief"] == 0.0
    assert pm["covered_contracts"] == 0
    assert pm["total"] == pytest.approx(pr.estimate_margin_short(0.05, 2.918), rel=1e-6)


# ---------- 跨到期月 / 数量池 ----------

def test_different_expiry_no_relief():
    legs = [("510050C2610M02700", 1, 0.10, 2.918),
            ("510050C2611M02800", -1, 0.05, 2.918)]
    pm = pr.estimate_portfolio_margin(legs)
    assert pm["relief"] == 0.0


def test_one_long_covers_one_short_only():
    # 1 张买腿 + 2 张卖腿 → 只覆盖 1 张，剩余 1 张维持单腿上界
    legs = [("510050C2610M02700", 1, 0.10, 2.918),
            ("510050C2610M02800", -2, 0.05, 2.918)]
    pm = pr.estimate_portfolio_margin(legs)
    full1 = pr.estimate_margin_short(0.05, 2.918)
    assert pm["covered_contracts"] == 1
    assert pm["total"] == pytest.approx(1000.0 + full1, rel=1e-6)


def test_long_qty_pool_multi_cover():
    # 3 张买腿全覆盖 2 张卖腿；长腿自身不占保证金
    legs = [("510050C2610M02700", 3, 0.10, 2.918),
            ("510050C2610M02800", -2, 0.05, 2.918)]
    pm = pr.estimate_portfolio_margin(legs)
    assert pm["total"] == pytest.approx(2000.0, rel=1e-6)
    assert pm["covered_contracts"] == 2


# ---------- fail-soft：不可解析码不抵扣 ----------

def test_unparseable_code_falls_back_to_single_leg():
    legs = [("STRADDLE K=3.0 2026-10-28", -2, 0.05, 2.918)]
    pm = pr.estimate_portfolio_margin(legs)
    assert pm["total"] == pytest.approx(pr.estimate_margin_short(0.05, 2.918) * 2, rel=1e-6)
    assert pm["relief"] == 0.0


def test_nearest_strike_picked_and_charge_capped():
    # 有两个可选买腿（2.650 / 2.750）→ 取最近的 2.750；价差 0.05×10000=500
    # 且 charge=min(500, 单腿上界) —— 价差过大时不多抵
    legs = [("510050C2610M02650", 1, 0.30, 2.918),
            ("510050C2610M02750", 1, 0.15, 2.918),
            ("510050C2610M02800", -1, 0.05, 2.918)]
    pm = pr.estimate_portfolio_margin(legs)
    assert pm["pairs"][0]["covered_by"] == 2.75
    assert pm["pairs"][0]["charge"] == pytest.approx(500.0, rel=1e-6)
    # 抵扣上限：charge ≤ 单腿全额
    assert pm["pairs"][0]["charge"] <= pm["pairs"][0]["full_margin"]


def test_empty_legs_and_long_only():
    assert pr.estimate_portfolio_margin([])["total"] == 0.0
    pm = pr.estimate_portfolio_margin([("510050C2610M02700", 2, 0.10, 2.918)])
    assert pm["total"] == 0.0 and pm["relief"] == 0.0
