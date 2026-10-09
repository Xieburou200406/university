# -*- coding: utf-8 -*-
"""§14 风险预算测试：情景构建、解析解夹逼、情景 PnL 量纲、降级链。合成数据，不联网。"""
import numpy as np
import pandas as pd
import pytest

from vol.riskbudget.optimize import (BudgetResult, portfolio_pnl_scenarios,
                                     solve_budget)
from vol.riskbudget.scenarios import build_scenarios


def make_panel(n=200, seed=5, trend=0.0):
    rng = np.random.default_rng(seed)
    iv = 0.25 + trend * np.arange(n) / n + 0.004 * np.sin(np.arange(n) / 9) + rng.normal(0, 0.002, n)
    spot = 2.7 + 0.05 * np.sin(np.arange(n) / 25) + np.cumsum(rng.normal(0, 0.0015, n))
    rv = pd.Series(spot).pct_change().rolling(20).std().bfill().to_numpy() * np.sqrt(245)
    dates = pd.date_range("2026-01-01", periods=n, freq="B").strftime("%Y-%m-%d")
    return pd.DataFrame({"date": dates, "spot": spot, "iv_near": iv,
                         "iv_next": iv + 0.01, "skew25": rng.normal(0, 0.004, n),
                         "rvol20": rv})


def test_build_scenarios_shapes_and_degrade():
    res = build_scenarios(make_panel(200), k=20, horizon=10)
    assert res["degraded"] is False and len(res["scenarios"]) == 20
    assert all("neighbor_date" in s and "delta_iv" in s and "delta_spot" in s
               for s in res["scenarios"])
    res_bad = build_scenarios(make_panel(80), k=20, horizon=10)
    assert res_bad["degraded"] is True and res_bad["scenarios"] == []


def test_solve_optimal_below_limit():
    pack = build_scenarios(make_panel(200, trend=0.06), k=20, horizon=10)   # IV 上行趋势
    res = solve_budget(cur_vega=0.0, cur_delta=0.0, theta_annual=400.0, scenario_pack=pack,
                       lam=4.0, vega_limit=881.0, delta_limit=15000.0, spot=2.7)
    assert isinstance(res, BudgetResult) and res.degraded is False
    assert res.target_vega > 0 or res.vega_state == "zero"    # IV 看涨 → 想持正 vega
    assert abs(res.target_vega) <= 881.0
    checks = {c["name"]: c["passed"] for c in res.checks}
    assert checks["vega_limit"] and checks["delta_limit"]


def test_limit_clipping_when_signal_strong():
    pack = build_scenarios(make_panel(200, trend=0.30), k=20, horizon=10)   # 强趋势
    res = solve_budget(0.0, 0.0, 400.0, pack, lam=1.0, vega_limit=500.0,
                       delta_limit=15000.0, spot=2.7)
    # 风控先于优化：目标被夹逼进限额内
    assert abs(res.target_vega) <= 500.0
    assert any(c["name"] == "vega_limit" and c["passed"] for c in res.checks)


def test_portfolio_pnl_math():
    scenarios = [{"delta_iv": 0.01, "delta_spot": 0.01, "neighbor_date": "x"},
                 {"delta_iv": -0.01, "delta_spot": -0.01, "neighbor_date": "y"}]
    pnl = portfolio_pnl_scenarios(delta=15000.0, vega=500.0, theta_annual=365.0,
                                  scenarios=scenarios, spot=2.7)
    dS = np.array([0.027, -0.027])
    expect = 15000.0 * dS + 500.0 * np.array([0.01, -0.01]) - 365.0 / 245
    assert np.allclose(pnl, expect)


def test_degraded_hold_current_exposure():
    pack = {"scenarios": [], "degraded": True, "reason": "面板不足"}
    res = solve_budget(-88.0, 1500.0, 400.0, pack, lam=4.0, vega_limit=881.0,
                       delta_limit=15000.0)
    assert res.degraded is True
    assert res.target_vega == -88.0 and res.target_delta == 1500.0    # 维持现仓位
    assert res.confidence == "LOW"
    assert any(c["name"] == "degraded_hold" for c in res.checks)


def test_lambda_levels_change_target():
    pack = build_scenarios(make_panel(200, trend=0.10), k=20, horizon=10)
    r_cons = solve_budget(0.0, 0.0, 0.0, pack, lam=8.0, vega_limit=881.0, delta_limit=15000.0)
    r_aggr = solve_budget(0.0, 0.0, 0.0, pack, lam=2.0, vega_limit=881.0, delta_limit=15000.0)
    # λ 越小越激进：目标敞口绝对值不小于保守档
    assert abs(r_aggr.target_vega) >= abs(r_cons.target_vega) - 1e-6
