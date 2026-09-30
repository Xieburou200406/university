# -*- coding: utf-8 -*-
"""
pricing.py — L2 定价层（demo 版）
BS 定价 + 希腊字母（解析式）+ Brent 反解 IV + 三次多项式曲线拟合。
量纲铁律：Vega = 每 1% IV 的价格变化；Theta = 每日。
"""
import math

from scipy.optimize import brentq
from scipy.stats import norm

SQRT_2PI = math.sqrt(2.0 * math.pi)


def _d1_d2(S, K, T, r, sigma):
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return d1, d1 - sigma * math.sqrt(T)


def bs_price(S, K, T, r, sigma, cp="C"):
    if T <= 0 or sigma <= 0:
        intrinsic = max(0.0, (S - K) if cp == "C" else (K - S))
        return intrinsic
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if cp == "C":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def greeks(S, K, T, r, sigma, cp="C"):
    """返回 (delta, gamma, vega, theta)。Vega 单位=每1%IV；Theta 单位=每日。"""
    if T <= 0 or sigma <= 0:
        delta = 1.0 if (cp == "C" and S > K) else (-1.0 if (cp == "P" and S < K) else 0.0)
        return delta, 0.0, 0.0, 0.0
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    pdf_d1 = math.exp(-0.5 * d1 * d1) / SQRT_2PI
    gamma = pdf_d1 / (S * sigma * math.sqrt(T))
    vega = S * pdf_d1 * math.sqrt(T) / 100.0          # 每 1% IV
    if cp == "C":
        delta = norm.cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * norm.cdf(d2)) / 365.0
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T))
                 + r * K * math.exp(-r * T) * norm.cdf(-d2)) / 365.0
    return delta, gamma, vega, theta


def implied_vol(price, S, K, T, r, cp="C", lo=0.01, hi=3.0):
    """Brent 反解 IV；区间外无解返回 None（禁止塞 0）。"""
    if price <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if cp == "C" else (K - S))
    disc = K * math.exp(-r * T)
    upper = S if cp == "C" else disc
    if not (intrinsic < price < upper):
        return None
    f = lambda s: bs_price(S, K, T, r, s, cp) - price  # noqa: E731
    try:
        return brentq(f, lo, hi, xtol=1e-6, maxiter=100)
    except ValueError:
        return None


def fit_curve_iv(strikes, ivs, S, T, r):
    """
    SVI 原始形拟合（主），失败自动降级三次多项式（兜底）。
    SVI: w(k) = a + b*(rho*(k-m) + sqrt((k-m)^2 + sigma^2))，w = iv^2*T，k = ln(K/F)
    返回 (callable(k)->iv, r_squared, model_name)。
    """
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
    # 兜底：三次多项式
    import numpy as np
    coef = np.polyfit(ks, ivs, deg=3)
    fitted = [float(np.polyval(coef, k)) for k in ks]
    mean_y = sum(ivs) / n
    ss_res = sum((y - f) ** 2 for y, f in zip(ivs, fitted))
    ss_tot = sum((y - mean_y) ** 2 for y in ivs) or 1e-12
    r2 = 1 - ss_res / ss_tot
    return (lambda k: float(np.polyval(coef, k))), r2, "poly3"


def _fit_svi(ks, ivs, T):
    import numpy as np
    from scipy.optimize import least_squares
    w = np.array([iv * iv * T for iv in ivs])
    k = np.array(ks)

    def resid(p):
        a, b, rho, m, sigma = p
        wv = a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + sigma ** 2))
        # 在 IV 尺度上拟合：总方差尺度太小（~1e-3），翅膀形状会被 a 项淹没
        return np.sqrt(np.maximum(wv, 1e-12) / T) - np.array(ivs)

    atm_w = float(w[np.argmin(np.abs(k))])
    best = None
    # 多起点重启：SVI 单起点常收敛到坏局部解（近平线）
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
        raise ValueError("SVI 所有起点均未收敛")
    a, b, rho, m, sigma = best.x
    # Gatheral 无蝶式套利必要条件
    if b > 4.0 / (T * (1 + abs(rho)) ** 2):
        raise ValueError("SVI 参数违反无蝶式套利约束")

    def fn(kk, a=a, b=b, rho=rho, m=m, sigma=sigma, T=T):
        wv = a + b * (rho * (kk - m) + math.sqrt((kk - m) ** 2 + sigma * sigma))
        return math.sqrt(max(wv, 1e-12) / T)

    fitted = [fn(kk) for kk in ks]
    mean_y = sum(ivs) / len(ivs)
    ss_res = sum((y - f) ** 2 for y, f in zip(ivs, fitted))
    ss_tot = sum((y - mean_y) ** 2 for y in ivs) or 1e-12
    return fn, 1 - ss_res / ss_tot


def tier_of(K, F, cp):
    """档位分类：|K/F-1|<2% ATM；否则虚值/实值。"""
    m = K / F - 1.0
    if abs(m) < 0.02:
        return "ATM"
    if cp == "C":
        return "OTM" if m > 0 else "ITM"
    return "OTM" if m < 0 else "ITM"


def liquidity_score(bid, ask, volume_rank, oi_rank):
    """
    流动性分 0-100：价差率 60% + 成交量分位 25% + 持仓量分位 15%。
    bid/ask 缺失或倒挂按最差档处理。
    """
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        spread_part = 0.0
    else:
        mid = (bid + ask) / 2
        ratio = (ask - bid) / mid
        spread_part = 1.0 if ratio <= 0.005 else max(0.0, 1.0 - (ratio - 0.005) / 0.095)
    return round(100 * (0.60 * spread_part + 0.25 * volume_rank + 0.15 * oi_rank))
