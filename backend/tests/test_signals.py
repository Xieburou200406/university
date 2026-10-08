# -*- coding: utf-8 -*-
"""铁律 5/7/12 用例：分位窗口防自包含、连续确认状态机、fail-closed 观望。"""
from vol.signals.percentile import PercentileEngine
from vol.signals.analog import AnalogEngine, build_features, synthetic_history


def _iv_hist(n=130, value=0.20):
    return [0.20] * (n - 1) + [value]


def test_insufficient_history():
    out = PercentileEngine(window=120).run([0.2] * 100)
    assert out["signal"] == "INSUFFICIENT_HISTORY"


def test_window_excludes_today():
    """铁律 5：分位窗口用截至昨日的数据。今天 = 窗口最高 → 分位 1.0。"""
    eng = PercentileEngine(window=120)
    out = eng.run(_iv_hist(value=0.30))
    assert out["iv_pct"] == 1.0


def test_two_day_confirmation():
    """铁律 12 配套：首日进高估区不出信号，连续 2 日才翻 SHORT_VOL。"""
    eng = PercentileEngine(window=120)
    d1 = eng.run(_iv_hist(value=0.30))
    assert d1["signal"] == "NEUTRAL" and d1["confirm_needed"] == 1
    d2 = eng.run(_iv_hist(value=0.30))
    assert d2["signal"] == "SHORT_VOL"


def test_analog_fail_closed_on_weak_edge():
    """铁律 7：胜率/下尾不达标的合成数据上引擎应输出观望而非硬选。"""
    dates, spot, atm_iv, nm_iv, rvol = synthetic_history(n=420, seed=7)
    feats = build_features(dates, atm_iv, rvol, nm_iv, spot)
    out = AnalogEngine(k=20, horizon=10).run(feats, spot, atm_iv)
    assert out["signal"] in ("买跨式(long straddle)", "卖跨式(short straddle,Δ对冲)",
                             "买日历(calendar long)", "单腿买Call", "观望（无共识）")
    if out["signal"] != "观望（无共识）":
        assert out["strategy_stats"][out["signal"]]["win"] >= 0.55
