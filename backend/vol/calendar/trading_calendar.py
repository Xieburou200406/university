# -*- coding: utf-8 -*-
"""交易日历 —— 把"哪些日子开市"载入机器（§18.2）。

数据来源优先级（§18.2 降级链）：
1. data/cache/trade_dates.csv（AKShare tool_trade_date_hist_sina 落盘的官方日历）→ 准确；
2. data/cache/ri_v2_*.csv 文件名日期（真实拉取过的交易日）→ 历史段准确；
3. 周末规则兜底（周一~周五视为交易日）→ 节假日未知，标记 degraded。

铁律：日历故障不许让管线崩（fail-loud-not-crash），但必须降级标记，
让前端状态条显示"日历降级（节假日未知）"，由人决定是否采信。
"""
from __future__ import annotations

import csv
import glob
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from vol.calendar.exchange_rules import EXEC_WINDOW

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CACHE_PATH = _REPO_ROOT / "data" / "cache" / "trade_dates.csv"
_PANEL_GLOB = str(_REPO_ROOT / "data" / "cache" / "ri_v2_*.csv")


@dataclass
class TradingCalendar:
    """交易日历。trade_dates=None 时退化为周末规则（degraded）。"""

    trade_dates: set[date] | None = None
    known_max: date | None = None
    degraded: bool = True
    source: str = "weekend-rule"
    reason: str = "无官方交易日历，按周末规则兜底（节假日未知）"

    # ---- 构造 ----

    @classmethod
    def from_dates(cls, dates: list[date], source: str = "csv-cache") -> "TradingCalendar":
        if not dates:
            return cls()
        ds = set(dates)
        return cls(trade_dates=ds, known_max=max(ds), degraded=False, source=source,
                   reason="")

    @classmethod
    def load_default(cls) -> "TradingCalendar":
        """按优先级加载：CSV 官方日历 → 面板缓存文件名 → 周末规则。"""
        p = Path(DEFAULT_CACHE_PATH)
        if p.exists():
            try:
                with open(p, newline="", encoding="utf-8") as f:
                    dates = [date.fromisoformat(row["date"]) for row in csv.DictReader(f)]
                if dates:
                    return cls.from_dates(dates, source="trade_dates.csv")
            except Exception:
                pass  # 坏文件按缺失处理，走降级链
        dates = _dates_from_panel_cache()
        if dates:
            cal = cls.from_dates(dates, source="panel-cache")
            cal.degraded = True
            cal.reason = "官方日历缺失：历史段按真实拉取日，未来日期按周末规则推断"
            return cal
        return cls()  # 全兜底

    # ---- 查询 ----

    def _is_weekend(self, d: date) -> bool:
        return d.weekday() >= 5

    def is_trading_day(self, d: date) -> bool:
        if self._is_weekend(d):
            return False
        if self.trade_dates is not None:
            if d in self.trade_dates:
                return True
            # 历史段（已知范围内）不在集合中 = 真实节假日
            if self.known_max is not None and d <= self.known_max:
                return False
            return True  # 未来日期：官方日历未覆盖时按工作日推断
        return True

    def next_trading_day(self, d: date) -> date:
        """严格晚于 d 的第一个交易日（valid_until 专用）。"""
        cur = d
        for _ in range(30):  # 春节连续休市也不会超过 30 天
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                return cur
        raise RuntimeError(f"30 天内找不到交易日（输入 {d}，calendar degraded={self.degraded}）")

    def prev_trading_day(self, d: date) -> date:
        cur = d
        for _ in range(30):
            cur -= timedelta(days=1)
            if self.is_trading_day(cur):
                return cur
        raise RuntimeError(f"30 天内找不到交易日（输入 {d}）")

    def trading_days_between(self, a: date, b: date) -> int:
        """(a, b] 区间内的交易日数（倒计时口径：不含今天、含到期日）。"""
        if b <= a:
            return 0
        n, cur = 0, a
        while cur < b:
            cur += timedelta(days=1)
            if self.is_trading_day(cur):
                n += 1
        return n

    def countdown(self, frm: date, expiry: date) -> int:
        """距到期的交易日数（frm 当天不算，到期日算 1）。"""
        return self.trading_days_between(frm, expiry)

    def exec_window(self, d: date) -> str:
        """T+1 执行窗口标签（铁律 8）：下一交易日的 9:35~10:00。"""
        return f"{self.next_trading_day(d)} {EXEC_WINDOW}"

    def status_label(self) -> str:
        base = f"交易日历：{self.source}"
        return base + "（降级：节假日未知）" if self.degraded else base


def _dates_from_panel_cache() -> list[date]:
    """从面板缓存文件名（ri_v2_YYYYMMDD.csv）提取真实拉取过的交易日。"""
    out = []
    for fp in glob.glob(_PANEL_GLOB):
        stem = os.path.basename(fp)[len("ri_v2_"):-len(".csv")]
        try:
            out.append(date(int(stem[:4]), int(stem[4:6]), int(stem[6:8])))
        except ValueError:
            continue
    return sorted(set(out))


# ---- 模块级单例（services / daily_job / panel 共享一份）----

_DEFAULT: TradingCalendar | None = None


def get_calendar(refresh: bool = False) -> TradingCalendar:
    global _DEFAULT
    if _DEFAULT is None or refresh:
        _DEFAULT = TradingCalendar.load_default()
    return _DEFAULT


def refresh_calendar_cache() -> bool:
    """显式联网刷新官方交易日历到 data/cache/trade_dates.csv。成功返回 True。"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        col = "trade_date" if "trade_date" in df.columns else df.columns[0]
        p = Path(DEFAULT_CACHE_PATH)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date"])
            for v in df[col]:
                d = v if isinstance(v, date) else date.fromisoformat(str(v)[:10])
                w.writerow([d.isoformat()])
        get_calendar(refresh=True)
        return True
    except Exception:
        return False
