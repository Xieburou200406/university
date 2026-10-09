# -*- coding: utf-8 -*-
"""vol.calendar —— 交易所规则载入引擎（§18：规则融入机器，不做教育页面）。

机器可读规则库 + 交易日历。所有决策路径（valid_until、执行窗口、到期倒计时、
晨检时刻）从这里取数，任何地方不得再手写 timedelta(days=1) 或"下一个自然日"。
"""
from vol.calendar.exchange_rules import (
    EXEC_WINDOW,
    EXPIRY_RULES,
    MORNING_CHECK_AT,
    MULTIPLIER_SSE_ETF,
    SESSIONS,
    fourth_wednesday,
    is_cffex_index_expiry,
    is_sse_etf_expiry,
    third_friday,
)
from vol.calendar.trading_calendar import TradingCalendar, get_calendar, refresh_calendar_cache

__all__ = [
    "EXEC_WINDOW", "EXPIRY_RULES", "MORNING_CHECK_AT", "MULTIPLIER_SSE_ETF",
    "SESSIONS", "fourth_wednesday", "third_friday",
    "is_sse_etf_expiry", "is_cffex_index_expiry",
    "TradingCalendar", "get_calendar", "refresh_calendar_cache",
]
