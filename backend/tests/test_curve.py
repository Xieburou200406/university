# -*- coding: utf-8 -*-
"""铁律 3 用例：C/P 聚合内建 + SVI 拟合质量 + BS 定价往返。"""
import math

import pandas as pd
import pytest

from vol.pricing.bs import bs_price, greeks, implied_vol
from vol.pricing.curve import CurveService, FitError


def _smile_chain():
    """合成近月链：同一行权价 C/P 各一条且 IV 差异大（复现 0.28 档 11.4%/15.6% 的坑）。"""
    spot, T, r = 2.7, 0.05, 0.02
    rows = []
    for k_pct in range(-6, 7):
        K = round(spot * (1 + k_pct * 0.02), 3)
        base = 0.20 + 0.9 * (math.log(K / spot)) ** 2 * 10   # U 型 smile
        for cp, bias in (("C", -0.02), ("P", +0.02)):
            rows.append({"code": f"510050{cp}2610M{int(K*1000):04d}",
                         "strike": K, "cp": cp, "iv_official": base + bias})
    return pd.DataFrame(rows), spot, T, r


def test_bs_iv_roundtrip():
    S, K, T, r, sigma = 2.7, 2.75, 0.05, 0.02, 0.25
    px = bs_price(S, K, T, r, sigma, "C")
    assert abs(implied_vol(px, S, K, T, r, "C") - sigma) < 1e-5


def test_greeks_scale():
    """vega 返回每 1.0 IV（小数）口径：ATM 平方根律 vega ≈ S√T·φ(0)/100? — 直接验数量级与符号。"""
    d, g, v, t = greeks(2.7, 2.7, 0.05, 0.02, 0.25, "C")
    assert 0 < d < 1 and g > 0 and v > 0 and t < 0
    # 每 1.0 IV 口径下 vega 应为"每 1% 口径"的 100 倍量级（ATM 1个月 ETF 期权 vega ≈ 0.002~0.01/1% → 0.2~1/1.0IV）
    assert 0.05 < v < 5.0


def test_curve_aggregates_cp_and_fits():
    chain, spot, T, r = _smile_chain()
    res = CurveService.build(chain, spot, T, r)
    # C/P 已聚合：每 strike 只剩一行
    assert res.agg["strike"].is_unique
    assert res.model in ("SVI", "poly3")
    assert res.r2 > 0.8   # 干净合成 smile 必须拟合良好


def test_curve_rejects_empty():
    empty = pd.DataFrame({"strike": [], "cp": [], "iv_official": []})
    with pytest.raises(FitError):
        CurveService.build(empty, 2.7, 0.05, 0.02)
