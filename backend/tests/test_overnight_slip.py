# -*- coding: utf-8 -*-
"""M8-① 回测隔夜滑点：换仓日按 T+1 开盘价口径，隔夜段不归属策略。
数据驱动传导系数 β(ΔIV~标的日收益)；缺开盘价/β不足 → 修正自动关闭（绝不虚构）。"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # 项目根（m2/m3 脚本层）

import numpy as np
import pandas as pd
import pytest

from m3_run import VEGA_NOTIONAL, backtest, estimate_iv_beta


def _df(n=200, beta_true=0.5, seed=7):
    """构造 ΔIV = β·ret + 噪声 的面板（date/spot/iv_near），供 β 估计与回测共用。"""
    rng = np.random.default_rng(seed)
    ret = rng.normal(0.0, 0.01, n)
    spot = 2.9 * np.cumprod(1.0 + ret)
    div = beta_true * ret + rng.normal(0, 0.0005, n)
    iv = 0.20 + np.cumsum(div)
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=n).strftime("%Y-%m-%d"),
        "spot": spot,
        "iv_near": iv,
    })


# ---------- β 估计：OLS 斜率还原构造系数 ----------

def test_estimate_iv_beta_recovers_true_slope():
    df = _df(beta_true=0.5)
    b = estimate_iv_beta(df)
    assert abs(b - 0.5) < 0.05


def test_estimate_iv_beta_insufficient_samples_returns_zero():
    df = _df(n=30)                                   # < IV_BETA_MIN_SAMPLES(60)
    assert estimate_iv_beta(df) == 0.0


# ---------- 回测滑点：默认关闭（向后兼容） ----------

def _states(df):
    n = len(df)
    pct = ["NEUTRAL"] + ["LONG_VOL"] * (n - 1)        # 仅第 2 日换仓（0→1）
    ana = [0] * n                                     # 类比全程 0，不干扰
    return pct, ana


def test_backtest_without_open_map_unchanged():
    df = _df(n=80)
    pct, ana = _states(df)
    curves = backtest(df, pct, ana)
    assert curves["percentile"][-1] == pytest.approx(
        backtest(df, pct, ana, open_map={"2099-01-01": 3.0}, iv_beta=1.0)["percentile"][-1])


def test_backtest_slippage_applied_on_switch_day():
    df = _df(n=80, seed=11)
    pct, ana = _states(df)
    base = backtest(df, pct, ana)["percentile"]
    om_val = 3.0                                       # T+1 开盘价（任意合理值）
    om = {df["date"].iloc[2]: om_val}
    stats = {}
    curves = backtest(df, pct, ana, open_map=om, iv_beta=0.5, stats_out=stats)
    # i=2 换仓 e=1：gap = om/spot[i-1] − 1，修正 = -e·β·gap·名义
    gap = om_val / float(df["spot"].iloc[1]) - 1.0
    slip = -1 * 0.5 * gap * VEGA_NOTIONAL
    assert curves["percentile"][2] == pytest.approx(base[2] + slip)
    # 换仓只此一次，之后两曲线平行
    diff = np.array(curves["percentile"][3:]) - np.array(base[3:])
    assert np.allclose(diff, slip)
    assert stats["switch_n"] == 1 and stats["slip_applied_n"] == 1
    assert stats["total_slip_pnl"] == pytest.approx(-slip)


def test_backtest_slippage_symmetric_short_vol():
    """空头换仓的隔夜修正方向相反（对称，不单边惩罚）。"""
    df = _df(n=80, seed=11)
    pct = ["NEUTRAL"] + ["SHORT_VOL"] * (len(df) - 1)
    ana = [0] * len(df)
    base = backtest(df, pct, ana)["percentile"]
    om_val = 3.0
    om = {df["date"].iloc[2]: om_val}
    curves = backtest(df, pct, ana, open_map=om, iv_beta=0.5)
    gap = om_val / float(df["spot"].iloc[1]) - 1.0
    assert curves["percentile"][2] == pytest.approx(base[2] + 0.5 * gap * VEGA_NOTIONAL)


def test_backtest_gap_missing_day_counted_not_crash():
    df = _df(n=80, seed=11)
    pct, ana = _states(df)
    stats = {}
    curves = backtest(df, pct, ana, open_map={"2099-01-01": 3.0}, iv_beta=0.5,
                      stats_out=stats)                # 无任何命中日期
    assert stats["slip_missing_n"] == 1 and stats["slip_applied_n"] == 0
    assert curves["percentile"] == backtest(df, pct, ana)["percentile"]


def test_backtest_zero_beta_disables_correction():
    df = _df(n=80, seed=11)
    pct, ana = _states(df)
    om = {df["date"].iloc[2]: 2.9 * 1.02}
    assert backtest(df, pct, ana, open_map=om, iv_beta=0.0)["percentile"] == \
        backtest(df, pct, ana)["percentile"]


def test_backtest_longvol_reference_never_slipped():
    """买入持有参照无换仓——隔夜滑点绝不影响 longvol 曲线。"""
    df = _df(n=80, seed=11)
    pct, ana = _states(df)
    om = {d: 3.0 for d in df["date"]}
    a = backtest(df, pct, ana)["longvol"]
    b = backtest(df, pct, ana, open_map=om, iv_beta=0.9)["longvol"]
    assert a == b


# ---------- fetch_open_map 失败契约：失败返 None，绝不抛出（fail-closed 上游） ----------

def test_fetch_open_map_returns_none_on_network_failure(monkeypatch):
    import sys as _sys
    m2 = _sys.modules.get("m2_run")
    assert m2 is not None, "项目根未入 sys.path"
    import types

    class _Boom:
        def __getattr__(self, name):
            raise ConnectionError("代理不可达")

    monkeypatch.setitem(_sys.modules, "akshare", _Boom())
    assert m2.fetch_open_map(days=30) is None


def test_fetch_open_map_none_propagates_to_backtest_failopen():
    """open_map=None → 滑点自动关闭（fail-open 留痕），与缺数据路径同分支。"""
    df = _df(n=80, seed=11)
    pct, ana = _states(df)
    stats = {}
    backtest(df, pct, ana, open_map=None, iv_beta=0.5, stats_out=stats)
    assert stats["slip_applied_n"] == 0 and stats["switch_n"] == 1
