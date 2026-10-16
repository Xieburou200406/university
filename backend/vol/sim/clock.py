# -*- coding: utf-8 -*-
"""回放虚拟时钟（§12.2 ReplayClock，日频诚实版）。"""
from datetime import date


class ReplayClock:
    """虚拟时钟：1 步 = 1 交易日（EOD）。所有 sim 组件只从时钟取 now()，
    禁止直接 datetime.now()（回放模式下会穿帮——信息墙铁律的时钟侧表达）。"""

    def __init__(self, days: list[date]):
        if not days:
            raise ValueError("回放需要至少 1 个交易日")
        self._days = sorted(days)
        self._i = 0

    def now(self) -> date:
        return self._days[self._i]

    def peek_next(self) -> date | None:
        """下一个交易日（只给日期不给数据——信息墙：数据由数据源按需截断）。"""
        return self._days[self._i + 1] if self._i + 1 < len(self._days) else None

    def step(self) -> bool:
        """前进一日。返回 True=已前进，False=已在最后一天（回放结束）。"""
        if self._i + 1 < len(self._days):
            self._i += 1
            return True
        return False

    @property
    def done(self) -> bool:
        return self._i >= len(self._days) - 1

    @property
    def days(self) -> list[date]:
        return list(self._days)
