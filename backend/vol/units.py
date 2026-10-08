# -*- coding: utf-8 -*-
"""量纲常量与换算 —— AGENTS.md 铁律 2 的唯一真源。
全库禁止在其他地方手写量纲系数；与 m3_run.py 实测标定逐位一致。"""

CONTRACT_UNIT = 10000  # 50ETF 期权合约乘数（张 → 元）


class Units:
    """上交所官方风险指标量纲（2026-10-08 实测标定，见 m3_run.py）。"""

    IV_AS_DECIMAL = 1.0        # iv=0.273 表示 27.3%
    VEGA_PER_1_0_IV = 1.0      # 官方 VEGA_VALUE：每 1.0 IV（小数 1.0 = 100 个 IV 点）
    THETA_ANNUAL = True        # 官方 THETA_VALUE：年化（非每日）

    @staticmethod
    def theta_daily(theta_annual: float) -> float:
        """年化 Theta → 每日 Theta（元/天/张，未乘合约乘数）。"""
        return theta_annual / 365.0

    @staticmethod
    def vega_pnl(vega_official: float, d_iv_decimal: float, unit: int = CONTRACT_UNIT) -> float:
        """单张 Vega 盈亏（元）：官方 vega × ΔIV(小数) × 合约乘数。"""
        return vega_official * d_iv_decimal * unit

    @staticmethod
    def delta_pnl(delta: float, d_spot: float, unit: int = CONTRACT_UNIT) -> float:
        """单张 Delta 盈亏（元）：delta × Δ标的价 × 合约乘数。"""
        return delta * d_spot * unit
