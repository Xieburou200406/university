# -*- coding: utf-8 -*-
"""策略模板（§12.2 StrategyTemplate/Registry，M6-① 内置 3 个，后续与类比引擎策略池同源扩容）。"""
from abc import ABC, abstractmethod
from datetime import date

from vol.sim.broker import SimLeg

STRIKE_GRID = 0.05        # 510050 期权行权价格格


def round_strike(x: float) -> float:
    return round(round(x / STRIKE_GRID) * STRIKE_GRID, 4)


class StrategyTemplate(ABC):
    id: str
    name: str

    @abstractmethod
    def generate_legs(self, spot: float, expiry: date, qty: int = 1,
                      params: dict | None = None) -> list[SimLeg]: ...


class SellStraddle(StrategyTemplate):
    id, name = "sell_straddle", "卖出跨式（收权利金）"

    def generate_legs(self, spot, expiry, qty=1, params=None):
        K = round_strike(params.get("K") if params and params.get("K") else spot)
        return [SimLeg("C", K, "sell", qty), SimLeg("P", K, "sell", qty)]


class BuyStraddle(StrategyTemplate):
    id, name = "buy_straddle", "买入跨式（做多波动率）"

    def generate_legs(self, spot, expiry, qty=1, params=None):
        K = round_strike(params.get("K") if params and params.get("K") else spot)
        return [SimLeg("C", K, "buy", qty), SimLeg("P", K, "buy", qty)]


class SellStrangle(StrategyTemplate):
    id, name = "sell_strangle", "卖出宽跨式（虚值收权利金）"

    def generate_legs(self, spot, expiry, qty=1, params=None):
        width = float((params or {}).get("width", 0.10))      # 默认虚值 10%
        return [SimLeg("C", round_strike(spot * (1 + width)), "sell", qty),
                SimLeg("P", round_strike(spot * (1 - width)), "sell", qty)]


REGISTRY: dict[str, StrategyTemplate] = {t.id: t for t in
                                         (SellStraddle(), BuyStraddle(), SellStrangle())}


def get_template(tpl_id: str) -> StrategyTemplate:
    if tpl_id not in REGISTRY:
        raise KeyError(f"未知模板 {tpl_id}，可选: {sorted(REGISTRY)}")
    return REGISTRY[tpl_id]
