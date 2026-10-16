# -*- coding: utf-8 -*-
"""回放编排（§12.2 ReplaySession，日频诚实版）。

信息墙：每个虚拟日只消费"当日及之前"的行情/IV 截面（df 按行供给，天然满足）。
定价一致铁律：同一虚拟日内，撮合价与估值价用同一 (spot, sigma) 截面。
"""
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from vol.sim.account import SimAccountService
from vol.sim.broker import R_FREE, SimOrder, SimFill, try_match
from vol.sim.clock import ReplayClock

JUMP_IV = 0.03            # IV 单日变动 > 3 个点 → 关键事件标注（§12.4 复盘打标）


@dataclass
class ReplayResult:
    nav: dict[int, list[tuple[date, float]]] = field(default_factory=dict)
    benchmark: list[tuple[date, float]] = field(default_factory=list)   # 买入持有标的
    fills: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)                    # IV 跳变等关键事件
    days: int = 0


class ReplaySession:
    """编排一次回放：时钟逐日推进 → 撮合到期订单 → 逐账户估值打点 → 基准对照。"""

    def __init__(self, svc: SimAccountService, r: float = R_FREE):
        self.svc = svc
        self.r = r

    def run(self, df: pd.DataFrame, orders: list[SimOrder],
            from_date: date, to_date: date, bench_cash: float = 10000.0) -> ReplayResult:
        d = df.copy()
        d["date"] = pd.to_datetime(d["date"]).dt.date
        d = d[(d["date"] >= from_date) & (d["date"] <= to_date)].sort_values("date")
        if d.empty:
            raise ValueError("回放区间内无面板数据（fail-closed）")
        clock = ReplayClock(list(d["date"]))
        rows = {r_["date"]: r_ for r_ in d.to_dict("records")}
        res = ReplayResult()
        spot0 = float(d["spot"].iloc[0])
        prev_iv: float | None = None
        pending = sorted(orders, key=lambda o: o.submit_date)

        while True:
            day = clock.now()
            row = rows[day]
            spot, sigma = float(row["spot"]), float(row["iv_near"])

            # IV 跳变事件（§12.4 复盘打标）
            if prev_iv is not None and abs(sigma - prev_iv) > JUMP_IV:
                res.events.append({"date": str(day), "kind": "iv_jump",
                                   "from": round(prev_iv, 4), "to": round(sigma, 4)})
            prev_iv = sigma

            # 撮合（submit_date ≤ 当日 的 pending 单；到期/限价未触由 try_match 处理）
            for o in pending:
                fills = try_match(o, day, spot, sigma, self.r)
                if fills:
                    self.svc.apply_fills(o.account_id, fills)
                    res.fills.extend({"date": str(f.filled_date), "account_id": o.account_id,
                                      "order_id": o.id, "cp": f.leg.cp, "strike": f.leg.strike,
                                      "side": f.leg.side, "qty": f.qty,
                                      "fill_px": f.fill_px, "cost": f.cost}
                                     for f in fills)

            # 逐账户估值
            for a in self.svc.accounts:
                self.svc.mark(a.id, day, spot, sigma, self.r)
                res.nav[a.id] = self.svc.nav_curve(a.id)

            # 基准：买入持有标的
            res.benchmark.append((day, round(bench_cash * spot / spot0, 2)))
            if not clock.step():
                break
        res.days = len(clock.days)
        return res
