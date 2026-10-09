# -*- coding: utf-8 -*-
"""交易所规则库（机器可读版）—— 来源：《三个交易所期权知识点》蒸馏为代码。

本模块是**规则的单一来源**（铁律 9）：vol.contracts 的到期日计算也委托到这里。
只放纯常量与纯函数，无任何 I/O，可独立单测。
三所对照速记：上交所/深交所 ETF 期权到期 = 到期月第 4 个星期三；
中金所股指期权（IO/MO/HO）到期 = 到期月第 3 个星期五。
"""
from datetime import date

# ---- 上交所 ETF 期权（本系统标的 510050 所在市场）----

#: 交易时段（24h 制 "HH:MM"）。集合竞价 9:15-9:25；连续竞价与股票一致。
SESSIONS = {
    "auction_am": ("09:15", "09:25"),
    "continuous_am": ("09:30", "11:30"),
    "continuous_pm": ("13:00", "15:00"),
}

#: §13 晨间复查触达时点：集合竞价结束、开盘前。
MORNING_CHECK_AT = "09:25"

#: T+1 执行窗口（铁律 8 的机器表达：信号 T 日收盘生成，T+1 开盘后半小时内有效执行）。
EXEC_WINDOW = "9:35~10:00"

#: 合约乘数（合约单位）。510050 ETF 期权 = 10000 份/张。
MULTIPLIER_SSE_ETF = 10000

#: 到期规则速查（供日志/界面提示引用；本系统只用第一条）。
EXPIRY_RULES = {
    "sse_etf": "到期月份第 4 个星期三（510050 等 ETF 期权）",
    "szse_etf": "到期月份第 4 个星期三（300ETF 等深市期权）",
    "cffex_index": "到期月份第 3 个星期五（IO/MO/HO 股指期权）",
}


def fourth_wednesday(year: int, month: int) -> date:
    """上交所/深交所 ETF 期权到期日：该月第 4 个星期三（铁律 4）。"""
    first = date(year, month, 1)
    first_wed = 1 + (2 - first.weekday()) % 7   # weekday(): Mon=0 ... Wed=2
    return date(year, month, first_wed + 21)


def third_friday(year: int, month: int) -> date:
    """中金所股指期权到期日：该月第 3 个星期五。"""
    first = date(year, month, 1)
    first_fri = 1 + (4 - first.weekday()) % 7   # weekday(): Fri=4
    return date(year, month, first_fri + 14)


def is_sse_etf_expiry(d: date) -> bool:
    return d == fourth_wednesday(d.year, d.month)


def is_cffex_index_expiry(d: date) -> bool:
    return d == third_friday(d.year, d.month)
