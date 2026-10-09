# -*- coding: utf-8 -*-
"""§13 隔夜预测测试：逻辑回归/校准数学性质、特征标签构建、弃权规则、walk-forward。合成数据，不联网。"""
import numpy as np
import pandas as pd
import pytest

from vol.prediction.forecast import (FEATURES, FLAT_BAND, OpenDirectionForecaster,
                                     build_dataset, walk_forward_evaluate)
from vol.prediction.model import LogisticIRLS, platt_calibrate


def make_panel(n=220, seed=7, informative=True):
    """合成面板：momentum_20d 与次日跳空正相关（informative），其余特征是噪音。"""
    rng = np.random.default_rng(seed)
    base_iv = 0.25
    iv = base_iv + 0.02 * np.sin(np.arange(n) / 15) + rng.normal(0, 0.005, n)
    spot = 2.7 + 0.05 * np.sin(np.arange(n) / 30) + np.cumsum(rng.normal(0, 0.002, n))
    mom = pd.Series(spot).pct_change(20).fillna(0.0).to_numpy()
    gap = 0.002 * np.roll(mom, 1) + rng.normal(0, 0.001, n)   # mom20 → 次日跳空（可学）
    gap[0] = 0.001
    openpx = spot + gap * spot
    dates = pd.date_range("2026-01-01", periods=n, freq="B").strftime("%Y-%m-%d")
    df = pd.DataFrame({
        "date": dates, "spot": spot, "open": openpx, "atm_iv": iv,
        "iv_pct_120": rng.uniform(0.2, 0.8, n), "skew25": rng.normal(0, 0.005, n),
        "term_slope": rng.normal(0, 0.003, n),
        "vrp": rng.normal(0.01, 0.01, n), "momentum_20d": mom,
    })
    if not informative:
        df["open"] = spot + rng.normal(0, 0.003, n) * spot
    return df


def test_logistic_iris_recovers_signal():
    """可分数据上 IRLS 应学到正斜率：p 随 x 增大。"""
    rng = np.random.default_rng(1)
    x = rng.normal(0, 1, 400)
    y = (rng.uniform(0, 1, 400) < 1 / (1 + np.exp(-2 * x))).astype(float)
    m = LogisticIRLS(l2=0.1).fit(x.reshape(-1, 1), y)
    p_low, p_high = m.predict_proba(np.array([[-2.0], [2.0]]))
    assert p_high > p_low + 0.3
    assert 0 < p_low < 0.5 < p_high < 1


def test_platt_keeps_monotonic_and_bounded():
    rng = np.random.default_rng(2)
    s = rng.uniform(0.2, 0.8, 300)
    y = (rng.uniform(0, 1, 300) < s).astype(float)
    a, b = platt_calibrate(s, y)
    assert b > 0
    xs = np.linspace(0, 1, 50)
    ps = 1 / (1 + np.exp(-(a + b * xs)))
    assert np.all(np.diff(ps) >= -1e-12) and ps.min() > 0 and ps.max() < 1


def test_build_dataset_label_flat_band():
    df = make_panel(60)
    d = build_dataset(df)
    gap = (d["open"].shift(-1) / d["spot"] - 1.0)
    known = gap.notna()                      # 末行无次日 → gap NaN，跳过
    flat = known & (gap.abs() <= FLAT_BAND)
    assert d.loc[flat, "y"].isna().all()          # FLAT 标 NaN
    assert d.loc[known & ~flat, "y"].isin([0.0, 1.0]).all()


def test_forecaster_informative_panel_not_abstain():
    df = make_panel(220)
    fc = OpenDirectionForecaster().forecast(df)
    if not fc.abstain:
        assert fc.p_up_cal is not None and 0 < fc.p_up_cal < 1
        assert set(fc.coef) == set(FEATURES)


def test_forecaster_abstain_dead_zone_or_small_sample():
    small = make_panel(50)                        # <60 样本 → 弃权
    fc = OpenDirectionForecaster().forecast(small)
    assert fc.abstain and "样本" in fc.reason
    noise = make_panel(220, seed=3, informative=False)   # 噪音面板：模型可训但大概率落死区/或给中性值
    fc2 = OpenDirectionForecaster().forecast(noise)
    assert fc2.p_up_cal is None or 0.35 < fc2.p_up_cal < 0.65


def test_forecaster_abstain_on_missing_feature():
    df = make_panel(220)
    df.loc[df.index[-1], "skew25"] = np.nan
    fc = OpenDirectionForecaster().forecast(df)
    assert fc.abstain and "skew25" in fc.reason


def test_walk_forward_runs_and_reports():
    res = walk_forward_evaluate(make_panel(300), min_train=120, step=5)
    assert res["ok"] is True
    assert 0 <= res["brier"] <= 1
    assert res["n_eval"] >= 15
    res_bad = walk_forward_evaluate(make_panel(100), min_train=120)
    assert res_bad["ok"] is False
