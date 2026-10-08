# -*- coding: utf-8 -*-
"""铁律 2 用例：官方希腊字母量纲换算（与 m3_run.py 实测标定逐位一致）。"""
from vol.units import Units


def test_theta_annual_to_daily():
    """官方 THETA 为年化：÷365。年化 -3650 → 每日 -10。"""
    assert Units.theta_daily(-3650.0) == -10.0


def test_vega_pnl_per_1_0_iv():
    """官方 VEGA 为每 1.0 IV（小数口径）：pnl = vega × ΔIV(小数) × 10000。
    ΔIV=0.01（1 个 IV 点）时 vega=2.5 → 250 元。"""
    assert Units.vega_pnl(2.5, 0.01) == 250.0


def test_delta_pnl():
    assert Units.delta_pnl(0.5, 0.02) == 100.0
