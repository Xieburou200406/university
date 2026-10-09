# -*- coding: utf-8 -*-
"""§18 交易所规则载入引擎：规则库纯函数 + 交易日历降级链 + 机器融入点。"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vol.calendar import (EXEC_WINDOW, TradingCalendar, fourth_wednesday,
                          get_calendar, is_cffex_index_expiry, is_sse_etf_expiry,
                          third_friday)
from api.services import _exec_window


# ---- 规则库：到期规则（与参考知识库 UsedExpiries 实证数据互相印证）----

def test_fourth_wednesday_known_dates():
    # 参考知识库部署文档 UsedExpiries=20261028 —— 上交所 ETF 期权 2026-10 到期日
    assert fourth_wednesday(2026, 10) == date(2026, 10, 28)
    assert fourth_wednesday(2026, 12) == date(2026, 12, 23)
    # 月初即周三的极端情形：2025-01-01 是周三 → 第 4 个周三 = 22
    assert fourth_wednesday(2025, 1) == date(2025, 1, 22)


def test_third_friday_cffex():
    # 参考知识库 UsedExpiries=20261218 —— 中金所 IO 2026-12 到期日（第 3 个星期五）
    assert third_friday(2026, 12) == date(2026, 12, 18)
    assert third_friday(2026, 10) == date(2026, 10, 16)


def test_expiry_predicates():
    assert is_sse_etf_expiry(date(2026, 10, 28))
    assert not is_sse_etf_expiry(date(2026, 10, 16))
    assert is_cffex_index_expiry(date(2026, 12, 18))
    assert not is_cffex_index_expiry(date(2026, 12, 23))


# ---- 交易日历：周末规则兜底（degraded）----

def test_weekend_fallback_next_prev():
    cal = TradingCalendar()  # 无数据 → 周末规则，degraded
    assert cal.degraded and cal.source == "weekend-rule"
    # 2026-10-09 是周五 → 下一交易日为周一 10-12
    assert cal.next_trading_day(date(2026, 10, 9)) == date(2026, 10, 12)
    assert cal.next_trading_day(date(2026, 10, 10)) == date(2026, 10, 12)  # 周六
    assert cal.prev_trading_day(date(2026, 10, 12)) == date(2026, 10, 9)
    assert not cal.is_trading_day(date(2026, 10, 10))


# ---- 交易日历：注入官方日历（节假日真实跳过）----

def test_official_calendar_holiday_skip():
    # 注入 2026-09-30 之后的下一个交易日是 10-09（10-01~10-08 国庆休市）
    cal = TradingCalendar.from_dates(
        [date(2026, 9, 30), date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 28)])
    assert not cal.degraded
    assert cal.next_trading_day(date(2026, 9, 30)) == date(2026, 10, 9)
    assert cal.is_trading_day(date(2026, 10, 9))
    assert not cal.is_trading_day(date(2026, 10, 2))          # 已知范围内缺失 = 真实节假日
    assert not cal.is_trading_day(date(2026, 10, 3))          # 周六
    assert cal.countdown(date(2026, 9, 30), date(2026, 10, 28)) == 3
    assert cal.countdown(date(2026, 10, 28), date(2026, 10, 28)) == 0


def test_exec_window_friday_signal():
    cal = TradingCalendar()  # 周五收盘信号 → 周一执行窗口（不是周六！）
    assert cal.exec_window(date(2026, 10, 9)) == f"2026-10-12 {EXEC_WINDOW}"


# ---- 机器融入点：services 执行窗口 + 面板缓存文件名降级源 ----

def test_services_exec_window_uses_trading_day():
    """回归锁（§18.3）：历史版本曾按"下一自然日"计算，周五收盘的信号会得到
    "周六 9:35~10:00" 这个无意义窗口。本断言保证执行窗口永远从【下一交易日】起算
    （周五→周一），任何人改回自然日逻辑，这条立即变红。现行为：永远是下一个交易日。"""
    assert _exec_window(date(2026, 10, 9)).startswith("2026-10-12 9:35~10:00")


def test_panel_cache_filename_source(tmp_path, monkeypatch):
    """无官方 CSV 时，用面板缓存文件名作为真实交易日来源（历史段准确）。"""
    import vol.calendar.trading_calendar as tc
    monkeypatch.setattr(tc, "DEFAULT_CACHE_PATH", tmp_path / "nope.csv")
    monkeypatch.setattr(tc, "_PANEL_GLOB", str(tmp_path / "ri_v2_*.csv"))
    for ymd in ("20260930", "20261009", "20261012"):
        (tmp_path / f"ri_v2_{ymd}.csv").write_text("code,iv\nx,1\n", encoding="utf-8")
    cal = tc.TradingCalendar.load_default()
    assert cal.source == "panel-cache" and cal.degraded   # 未来日期仍靠推断
    assert cal.next_trading_day(date(2026, 9, 30)) == date(2026, 10, 9)   # 国庆真实跳过
    assert not cal.is_trading_day(date(2026, 10, 2))


def test_get_calendar_singleton():
    assert get_calendar() is get_calendar()
