# -*- coding: utf-8 -*-
"""M8-④ 全量历史复现：walk_forward_yearly 聚合逻辑单测（伪造引擎输出，绝不触网）。"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # 项目根（m8_run 脚本层）

import numpy as np
import pandas as pd
import pytest

import m8_run


def _df(dates, iv):
    n = len(iv)
    return pd.DataFrame({
        "date": dates,
        "spot": np.full(n, 2.9),
        "iv_near": iv,
        "iv_next": iv,
        "skew25": np.full(n, 0.02),
        "rvol20": np.full(n, 0.15),
    })


def _dates(n, start="2020-01-01"):
    return [str(d)[:10] for d in pd.bdate_range(start, periods=n)]


def test_walk_forward_yearly_known_pattern(monkeypatch):
    """IV 每 10 日 +1 点再 -1 点往复（|Δ|>0.001），伪造类比引擎永远看多：
    涨日命中、跌日全错 → 逐年数字可手算。"""
    # WINDOW=120, HORIZON=10：构造 iv[i+10]-iv[i] 方向交替
    n = 400
    iv = []
    v = 0.20
    for i in range(n):
        iv.append(v)
        if i % 10 == 9:
            v += 0.02 if (i // 10) % 2 == 0 else -0.02
        else:
            v += 0.0001  # 微动 < 0.001 阈值不参与
    df = _df(_dates(n, start="2020-01-01"), iv)
    monkeypatch.setattr(m8_run, "analog_states", lambda d: [1] * n)
    monkeypatch.setattr(m8_run, "percentile_states", lambda iv_: ["NEUTRAL"] * n)
    out = m8_run.walk_forward_yearly(df)
    assert len(out) >= 1
    total_ana = int(out["samples"].sum())
    # 换向点：每 10 天一个 ±0.02 的跳变；跳变日 i 满足 (i%10)==9 → i+10 也满足
    # 若连续两段同向（+0.02 后 -0.02 交替），i 与 i+10 的 Δiv 符号交替 → 看多命中一半
    assert total_ana > 0
    hit = out["ana_hit"].dropna()
    assert hit.between(0, 1).all()
    # 朴素基准 ≥ 0.5（定义保证）
    assert (out["naive"] >= 0.5 - 1e-9).all()


def test_walk_forward_yearly_skips_tiny_moves(monkeypatch):
    """|ΔIV|≤0.001 全程 → 零样本，返回全 NaN 而不崩溃。"""
    n = 200
    iv = [0.20 + 0.0005 * (i % 2) for i in range(n)]
    df = _df(_dates(n), iv)
    monkeypatch.setattr(m8_run, "analog_states", lambda d: [1] * n)
    monkeypatch.setattr(m8_run, "percentile_states", lambda iv_: ["NEUTRAL"] * n)
    out = m8_run.walk_forward_yearly(df)
    assert (out["samples"] == 0).all()
    assert out["ana_hit"].isna().all()
    assert out["naive"].eq(0.5).all()


def test_yearly_focus_month_grouping(monkeypatch):
    """极端年过滤：只统计指定年份、按月聚合、样本数守恒。"""
    n = 500
    iv = []
    v = 0.20
    for i in range(n):
        iv.append(v)
        if i % 10 == 9:
            v += 0.02
    df = _df(_dates(n, start="2020-01-01"), iv)   # 500 交易日 ≈ 2020-2021 底
    monkeypatch.setattr(m8_run, "analog_states", lambda d: [1] * n)
    focus = m8_run.yearly_focus(df, years=("2020",))
    assert len(focus) > 0
    assert (focus["month"].str.startswith("2020")).all()
    assert (focus["hit_rate"] == 1.0).all()       # 单边上行 + 看多 → 全对
