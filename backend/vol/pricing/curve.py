# -*- coding: utf-8 -*-
"""波动率曲线拟合（从 pricing.py 原样迁移 + C/P 聚合内建）。
铁律 3：C/P 聚合由 CurveService 强制执行，调用方拿不到未聚合路径。"""
import math

import numpy as np


class FitError(ValueError):
    pass


def fit_curve_iv(strikes, ivs, S, T, r):
    """SVI（主）失败自动降级三次多项式（兜底）。返回 (callable(k)->iv, r2, model_name)。"""
    F = S * math.exp(r * T)
    ks = [math.log(K / F) for K in strikes]
    n = len(ks)
    if n < 4:
        avg = sum(ivs) / n
        return (lambda k: avg), 0.0, "degenerate"
    try:
        fn, r2 = _fit_svi(ks, ivs, T)
        if r2 > 0.5:
            return fn, r2, "SVI"
    except Exception:
        pass
    coef = np.polyfit(ks, ivs, deg=3)
    fitted = [float(np.polyval(coef, k)) for k in ks]
    mean_y = sum(ivs) / n
    ss_res = sum((y - f) ** 2 for y, f in zip(ivs, fitted))
    ss_tot = sum((y - mean_y) ** 2 for y in ivs) or 1e-12
    return (lambda k: float(np.polyval(coef, k))), 1 - ss_res / ss_tot, "poly3"


def _fit_svi(ks, ivs, T):
    from scipy.optimize import least_squares
    w = np.array([iv * iv * T for iv in ivs])
    k = np.array(ks)

    def resid(p):
        a, b, rho, m, sigma = p
        wv = a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + sigma ** 2))
        # IV 尺度残差：总方差尺度 ~1e-3 会被 a 项淹没（铁律 3 配套教训）
        return np.sqrt(np.maximum(wv, 1e-12) / T) - np.array(ivs)

    atm_w = float(w[np.argmin(np.abs(k))])
    best = None
    for b0 in (0.01, 0.05, 0.2):
        for rho0 in (-0.5, 0.0, 0.5):
            for m0 in (-0.05, 0.0, 0.05):
                p0 = [atm_w * 0.9, b0, rho0, m0, 0.1]
                try:
                    res = least_squares(resid, p0, bounds=([0.0, 0.0, -0.999, -1.0, 1e-4],
                                                           [max(atm_w * 3, 0.2), 2.0, 0.999, 1.0, 2.0]),
                                        max_nfev=1000)
                except Exception:
                    continue
                if best is None or res.cost < best.cost:
                    best = res
    if best is None:
        raise FitError("SVI 所有起点均未收敛")
    a, b, rho, m, sigma = best.x
    if b > 4.0 / (T * (1 + abs(rho)) ** 2):   # Gatheral 无蝶式套利必要条件
        raise FitError("SVI 参数违反无蝶式套利约束")

    def fn(kk, a=a, b=b, rho=rho, m=m, sigma=sigma, T=T):
        wv = a + b * (rho * (kk - m) + math.sqrt((kk - m) ** 2 + sigma * sigma))
        return math.sqrt(max(wv, 1e-12) / T)

    fitted = [fn(kk) for kk in ks]
    mean_y = sum(ivs) / len(ivs)
    ss_res = sum((y - f) ** 2 for y, f in zip(ivs, fitted))
    ss_tot = sum((y - mean_y) ** 2 for y in ivs) or 1e-12
    return fn, 1 - ss_res / ss_tot


def aggregate_cp(df, price_col="iv_official"):
    """同行权价 C/P 聚合（铁律 3）。输入须含 strike 与 iv 列；返回按 strike 排序的均值表。"""
    col = "iv" if price_col not in df.columns else price_col
    return (df[df[col] > 0]
            .groupby("strike", as_index=False)[col].mean()
            .sort_values("strike").reset_index(drop=True))


class CurveService:
    """曲线构建入口。聚合内建，返回 CurveResult。"""

    @staticmethod
    def build(chain, spot, T, r, iv_col="iv_official"):
        agg = aggregate_cp(chain, iv_col)
        if agg.empty:
            raise FitError("无有效 IV 数据")
        F = spot * math.exp(r * T)
        ks = [math.log(K / F) for K in agg["strike"]]
        fn, r2, model = fit_curve_iv(agg["strike"].tolist(), agg[iv_col].tolist(), spot, T, r)
        return CurveResult(fn=fn, r2=r2, model=model, agg=agg, forward=F, T=T)


class CurveResult:
    def __init__(self, fn, r2, model, agg, forward, T):
        self.fn = fn
        self.r2 = r2
        self.model = model
        self.agg = agg
        self.forward = forward
        self.T = T

    def iv_at_strike(self, K):
        return self.fn(math.log(K / self.forward))

    def deviation_bp(self, K, iv_market):
        return (iv_market - self.iv_at_strike(K)) * 10000.0
