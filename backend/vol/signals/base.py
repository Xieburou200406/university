# -*- coding: utf-8 -*-
"""信号层 —— 引擎接口（AGENTS.md §4.4）。
铁律 5：MarketContext.iv_history / features 只含 ≤ 当日数据，检索切片由引擎内保证。"""
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class MarketContext:
    date: object                       # 当日 date
    spot: float
    iv_history: list                   # ATM IV 序列（升序，最后一位=今天），只含 ≤date 数据
    features: list = field(default_factory=list)   # 类比特征 dict 列表（同序）
    rvol20: list = field(default_factory=list)
    next_month_iv: list = field(default_factory=list)
    spot_history: list = field(default_factory=list)


class SignalEngine(Protocol):
    name: str

    def run(self, ctx: MarketContext) -> dict: ...
