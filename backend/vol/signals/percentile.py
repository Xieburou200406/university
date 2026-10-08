# -*- coding: utf-8 -*-
"""引擎 A：IV 滚动分位数 + 连续确认状态机（从 signals.py 原样迁移）。"""


class PercentileEngine:
    def __init__(self, window=120, confirm_days=2):
        self.window = window
        self.confirm_days = confirm_days
        self.state = "NEUTRAL"
        self._streak = 0

    def run(self, atm_iv_series):
        """atm_iv_series: list[float]（日期升序，最后一位=今天）。"""
        s = [v for v in atm_iv_series if v is not None]
        if len(s) < self.window:
            return {"engine": "percentile", "signal": "INSUFFICIENT_HISTORY",
                    "detail": f"历史不足 {self.window} 日，当前 {len(s)} 日"}
        hist = s[-(self.window + 1):-1]   # 窗口截至昨日，防自包含（铁律 5）
        today = s[-1]
        pct = sum(1 for v in hist if v <= today) / len(hist)

        zone = "HIGH" if pct >= 0.80 else ("LOW" if pct <= 0.20 else "MID")
        if zone != "MID" and zone == getattr(self, "_last_zone", None):
            self._streak += 1
        elif zone != "MID":
            self._streak = 1
        else:
            self._streak = 0
        self._last_zone = zone

        if self._streak >= self.confirm_days:
            self.state = "SHORT_VOL" if zone == "HIGH" else "LONG_VOL"
        elif zone == "MID":
            self.state = "NEUTRAL"

        return {"engine": "percentile", "signal": self.state, "iv_pct": round(pct, 3),
                "iv_now": round(today, 4), "window": self.window,
                "streak": self._streak, "confirm_needed": max(0, self.confirm_days - self._streak)}
