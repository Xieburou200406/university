# -*- coding: utf-8 -*-
"""档位分类与流动性评分（从 pricing.py 原样迁移）。"""


def tier_of(K, F, cp):
    """档位分类：|K/F-1|<2% ATM；否则虚值/实值。"""
    m = K / F - 1.0
    if abs(m) < 0.02:
        return "ATM"
    if cp == "C":
        return "OTM" if m > 0 else "ITM"
    return "OTM" if m < 0 else "ITM"


def liquidity_score(bid, ask, volume_rank, oi_rank):
    """流动性分 0-100：价差率 60% + 成交量分位 25% + 持仓量分位 15%。
    bid/ask 缺失或倒挂按最差档处理。"""
    if not bid or not ask or bid <= 0 or ask <= 0 or ask < bid:
        spread_part = 0.0
    else:
        mid = (bid + ask) / 2
        ratio = (ask - bid) / mid
        spread_part = 1.0 if ratio <= 0.005 else max(0.0, 1.0 - (ratio - 0.005) / 0.095)
    return round(100 * (0.60 * spread_part + 0.25 * volume_rank + 0.15 * oi_rank))
