# -*- coding: utf-8 -*-
"""均值-方差解析解（§14.2）：v* = μ/(λσ²) 再夹逼硬限额——风控先于优化。
组合 v1 按单因子分别求解（vega/delta），协方差修正留 v2；量纲全部铁律 2 口径。"""
from dataclasses import dataclass, field

import numpy as np

TRADING_DAYS = 245


def portfolio_pnl_scenarios(delta: float, vega: float, theta_annual: float,
                            scenarios: list[dict], spot: float,
                            gamma: float = 0.0, vanna: float = 0.0) -> np.ndarray:
    """情景 PnL（元）：PnL_s ≈ delta·ΔS + 0.5·gamma·ΔS² + vega·ΔIV + 0.5·vanna·ΔS·ΔIV + theta/245。
    delta/vega/gamma/vanna 为组合口径（元），theta_annual 年化（铁律 2）。
    ΔS = 情景比例 × 当前 spot（delta 元口径对应标的每 1 元变动）。"""
    if not scenarios:
        return np.zeros(0)
    d_iv = np.array([s["delta_iv"] for s in scenarios])
    d_sp = np.array([s["delta_spot"] for s in scenarios]) * spot
    pnl = (delta * d_sp + 0.5 * gamma * d_sp ** 2
           + vega * d_iv + 0.5 * vanna * d_sp * d_iv
           - theta_annual / TRADING_DAYS)
    return pnl


def _solve_single(mu: float, sigma: float, lam: float, limit: float) -> tuple[float, str]:
    """单因子解析解：v* = μ/(λσ²)，夹逼 ±limit。σ≈0 或 μ=0 → 0。"""
    if sigma < 1e-9 or abs(mu) < 1e-12:
        return 0.0, "zero"
    v = mu / (lam * sigma ** 2)
    if abs(v) > limit:
        return float(np.sign(v) * limit), "clipped"
    return float(v), "optimal"


def _confidence(mu: float, sigma: float) -> str:
    """情景离散度 vs 期望 → 置信度档位（§14.2）。"""
    if sigma < 1e-9:
        return "LOW"
    if sigma < 0.5 * abs(mu):
        return "HIGH"
    if sigma < abs(mu):
        return "MID"
    return "LOW"


@dataclass
class BudgetResult:
    lambda_level: str
    target_vega: float
    vega_state: str                 # optimal / clipped / zero
    target_delta: float
    delta_state: str
    cvar5: float                    # 当前持仓在情景下的 CVaR5%（元，负=亏损）
    confidence: str                 # HIGH / MID / LOW
    degraded: bool
    reason: str
    checks: list = field(default_factory=list)


def solve_budget(cur_vega: float, cur_delta: float, theta_annual: float,
                 scenario_pack: dict, lam: float, vega_limit: float,
                 delta_limit: float, lambda_level: str = "balanced",
                 spot: float = 3.0, gamma: float = 0.0, vanna: float = 0.0) -> BudgetResult:
    """求解今日风险预算。
    - 降级（§16.4）：degraded 时情景退化为零变动 → 目标敞口=当前（维持现仓），只算持有成本。"""
    scenarios = scenario_pack.get("scenarios", [])
    if scenario_pack.get("degraded") or not scenarios:
        pnl = np.array([-theta_annual / TRADING_DAYS])
        cvar5 = float(pnl.min())
        return BudgetResult(lambda_level, cur_vega, "hold", cur_delta, "hold",
                            cvar5, "LOW", True,
                            scenario_pack.get("reason", "无情景，维持现仓位"),
                            [{"name": "degraded_hold", "passed": True,
                              "detail": "降级：维持现仓位（fail-closed，不撒谎）"}])
    mu_iv, sig_iv = scenario_pack["mu_iv"], scenario_pack["sigma_iv"]
    mu_sp, sig_sp = scenario_pack["mu_spot"], scenario_pack["sigma_spot"]
    # 期望收益换算到敞口单位：vega 1 单位赚 mu_iv 元；delta 1 单位赚 mu_sp（比例）元
    tv, tv_state = _solve_single(mu_iv, sig_iv, lam, vega_limit)
    td, td_state = _solve_single(mu_sp, sig_sp, lam, delta_limit)
    conf_v, conf_d = _confidence(mu_iv, sig_iv), _confidence(mu_sp, sig_sp)
    confidence = {"HIGH": 2, "MID": 1, "LOW": 0}[min(conf_v, conf_d, key=lambda c: {"HIGH": 2, "MID": 1, "LOW": 0}[c])]
    confidence = {2: "HIGH", 1: "MID", 0: "LOW"}[confidence]
    pnl = portfolio_pnl_scenarios(cur_delta, cur_vega, theta_annual, scenarios, spot, gamma, vanna)
    cvar5 = float(pnl[pnl <= np.quantile(pnl, 0.05)].mean()) if len(pnl) else 0.0
    checks = [
        {"name": "vega_limit", "passed": abs(tv) <= vega_limit + 1e-9,
         "detail": f"目标 {tv:.1f} vs 限额 ±{vega_limit}（{tv_state}）"},
        {"name": "delta_limit", "passed": abs(td) <= delta_limit + 1e-9,
         "detail": f"目标 {td:.1f} vs 限额 ±{delta_limit}（{td_state}）"},
    ]
    return BudgetResult(lambda_level, round(tv, 1), tv_state, round(td, 1), td_state,
                        round(cvar5, 1), confidence, False, "OK", checks)
