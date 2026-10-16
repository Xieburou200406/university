# -*- coding: utf-8 -*-
"""模拟账户服务（§12.2 SimAccountService，单机内存版）。

持仓键 = (cp, strike)；同腿跨到期合并（回放内到期日通常一致，简化）。
净值 = 现金 + Σ 持仓市值（qty 带符号，空头 MV 为负——与正式账本 mv 口径一致）。
"""
from dataclasses import dataclass, field
from datetime import date

from vol.sim.broker import R_FREE, SimFill, option_px


@dataclass
class SimAccount:
    id: int
    name: str
    cash: float
    auto_mirror: bool = False
    positions: dict[tuple, int] = field(default_factory=dict)   # (cp, strike) -> signed qty
    strikes: dict[tuple, float] = field(default_factory=dict)   # key -> strike（同 cp 撞键防丢）
    expiries: dict[tuple, date] = field(default_factory=dict)   # key -> expiry


class SimAccountService:
    def __init__(self) -> None:
        self._accounts: dict[int, SimAccount] = {}
        self._nav: dict[int, list[tuple[date, float]]] = {}
        self._next_id = 1

    # ---- 账户 ----
    def create(self, name: str, cash0: float, auto_mirror: bool = False) -> SimAccount:
        a = SimAccount(id=self._next_id, name=name, cash=float(cash0), auto_mirror=auto_mirror)
        self._accounts[a.id] = a
        self._nav[a.id] = []
        self._next_id += 1
        return a

    def get(self, account_id: int) -> SimAccount:
        return self._accounts[account_id]

    @property
    def accounts(self) -> list[SimAccount]:
        return list(self._accounts.values())

    # ---- 撮合结果入账 ----
    def apply_fills(self, account_id: int, fills: list[SimFill]) -> None:
        a = self.get(account_id)
        for f in fills:
            k = f.leg.key()
            signed = f.qty if f.leg.side == "buy" else -f.qty
            a.positions[k] = a.positions.get(k, 0) + signed
            a.strikes[k] = f.leg.strike
            if f.leg.expiry is not None:
                a.expiries[k] = f.leg.expiry
            if a.positions[k] == 0:
                a.positions.pop(k, None)               # 平光即销（防僵尸腿）
            a.cash += f.cash_delta()

    # ---- 估值 ----
    def mark(self, account_id: int, virtual_date: date, spot: float, sigma: float,
             r: float = R_FREE) -> float:
        """EOD 估值并打点。返回 nav。仓位 T ≤ 0（已到期未行权处理）按内在价值清算。"""
        a = self.get(account_id)
        mv = 0.0
        dead = []
        for k, qty in a.positions.items():
            cp, K = k
            expiry = a.expiries.get(k)
            T = max((expiry - virtual_date).days, 1) / 365.0 if expiry else 1.0 / 365.0
            if expiry is not None and virtual_date >= expiry:   # 到期：内在价值结算
                intrinsic = max(spot - K, 0.0) if cp == "C" else max(K - spot, 0.0)
                px, dead = intrinsic, dead + [k]
            else:
                px = option_px(spot, K, T, sigma, cp, r)
            mv += qty * px * 10000.0
        for k in dead:
            a.positions.pop(k, None)                            # 到期了结（欧式现金交割简化）
        nav = a.cash + mv
        self._nav[account_id].append((virtual_date, round(nav, 2)))
        return nav

    def nav_curve(self, account_id: int) -> list[tuple[date, float]]:
        return list(self._nav.get(account_id, []))
