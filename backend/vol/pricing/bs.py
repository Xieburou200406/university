# -*- coding: utf-8 -*-
"""BS 定价与希腊字母（从 pricing.py 原样迁移，行为逐位一致）。
量纲：输入 iv 为小数；返回 vega 为每 1.0 IV、theta 为年化（与官方指标同口径）。"""
import math

from scipy.optimize import brentq
from scipy.stats import norm

SQRT_2PI = math.sqrt(2.0 * math.pi)


def _d1_d2(S, K, T, r, sigma):
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    return d1, d1 - sigma * math.sqrt(T)


def bs_price(S, K, T, r, sigma, cp="C"):
    if T <= 0 or sigma <= 0:
        return max(0.0, (S - K) if cp == "C" else (K - S))
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    if cp == "C":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)


def greeks(S, K, T, r, sigma, cp="C"):
    """返回 (delta, gamma, vega, theta)。vega=每 1.0 IV（小数口径）；theta=年化。"""
    if T <= 0 or sigma <= 0:
        delta = 1.0 if (cp == "C" and S > K) else (-1.0 if (cp == "P" and S < K) else 0.0)
        return delta, 0.0, 0.0, 0.0
    d1, d2 = _d1_d2(S, K, T, r, sigma)
    pdf_d1 = math.exp(-0.5 * d1 * d1) / SQRT_2PI
    gamma = pdf_d1 / (S * sigma * math.sqrt(T))
    vega = S * pdf_d1 * math.sqrt(T)                 # 每 1.0 IV（小数）
    if cp == "C":
        delta = norm.cdf(d1)
        theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T))
                 - r * K * math.exp(-r * T) * norm.cdf(d2))
    else:
        delta = norm.cdf(d1) - 1.0
        theta = (-(S * pdf_d1 * sigma) / (2 * math.sqrt(T))
                 + r * K * math.exp(-r * T) * norm.cdf(-d2))
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
