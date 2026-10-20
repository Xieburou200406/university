# -*- coding: utf-8 -*-
"""策略模板（§12.2 StrategyTemplate/Registry，P3 扩容：3 → 8 个内置模板）。
与类比引擎策略池同源的原则：模板展开的腿结构与引擎建议的策略形态一致，学练不分裂。
日历价差需要腿级到期日（SimLeg.expiry），撮合/估值/归因全链路已支持。"""
from abc import ABC, abstractmethod
from datetime import date, timedelta

from vol.calendar.exchange_rules import fourth_wednesday
from vol.sim.broker import SimLeg

STRIKE_GRID = 0.05        # 510050 期权行权价格格


def round_strike(x: float) -> float:
    return round(round(x / STRIKE_GRID) * STRIKE_GRID, 4)


def prev_monthly_expiry(expiry: date) -> date:
    """expiry 之前最近的月度到期日（第四个星期三）——日历价差的近月腿。"""
    probe = expiry - timedelta(days=28)
    near = fourth_wednesday(probe.year, probe.month)
    while near >= expiry:                     # 极端边界（月末撞第四个周三）再往前推一个月
        probe = probe.replace(day=1) - timedelta(days=1)
        near = fourth_wednesday(probe.year, probe.month)
    return near


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


class BuyStrangle(StrategyTemplate):
    id, name = "buy_strangle", "买入宽跨式（做多波动率，虚值两翼）"

    def generate_legs(self, spot, expiry, qty=1, params=None):
        width = float((params or {}).get("width", 0.10))
        return [SimLeg("C", round_strike(spot * (1 + width)), "buy", qty),
                SimLeg("P", round_strike(spot * (1 - width)), "buy", qty)]


class BullCallSpread(StrategyTemplate):
    id, name = "bull_call_spread", "牛市看涨价差（买低卖高 Call）"

    def generate_legs(self, spot, expiry, qty=1, params=None):
        w = float((params or {}).get("width", STRIKE_GRID))
        k_low = round_strike(params.get("K") if params and params.get("K") else spot)
        return [SimLeg("C", k_low, "buy", qty),
                SimLeg("C", round_strike(k_low + w), "sell", qty)]


class BearPutSpread(StrategyTemplate):
    id, name = "bear_put_spread", "熊市看跌价差（买高卖低 Put）"

    def generate_legs(self, spot, expiry, qty=1, params=None):
        w = float((params or {}).get("width", STRIKE_GRID))
        k_high = round_strike(params.get("K") if params and params.get("K") else spot)
        return [SimLeg("P", k_high, "buy", qty),
                SimLeg("P", round_strike(k_high - w), "sell", qty)]


class CalendarSpread(StrategyTemplate):
    id, name = "calendar_spread", "日历价差（卖近月买远月同行权价 Call）"

    def generate_legs(self, spot, expiry, qty=1, params=None):
        K = round_strike(params.get("K") if params and params.get("K") else spot)
        near = prev_monthly_expiry(expiry)            # 近月腿：腿级到期日（撮合/估值全链路支持）
        return [SimLeg("C", K, "sell", qty, expiry=near),
                SimLeg("C", K, "buy", qty, expiry=expiry)]


class IronCondor(StrategyTemplate):
    id, name = "iron_condor", "铁鹰（双翼虚值收权利金，风险有限）"

    def generate_legs(self, spot, expiry, qty=1, params=None):
        w = float((params or {}).get("width", 0.10))          # 短腿虚值
        wing = float((params or {}).get("wing", 0.05))        # 保护翼
        K = round_strike(params.get("K") if params and params.get("K") else spot)
        k_c, k_p = round_strike(K * (1 + w)), round_strike(K * (1 - w))
        return [SimLeg("C", k_c, "sell", qty),
                SimLeg("C", round_strike(k_c + wing), "buy", qty),
                SimLeg("P", k_p, "sell", qty),
                SimLeg("P", round_strike(k_p - wing), "buy", qty)]


REGISTRY: dict[str, StrategyTemplate] = {t.id: t for t in (
    SellStraddle(), BuyStraddle(), SellStrangle(), BuyStrangle(),
    BullCallSpread(), BearPutSpread(), CalendarSpread(), IronCondor())}


def get_template(tpl_id: str) -> StrategyTemplate:
    if tpl_id not in REGISTRY:
        raise KeyError(f"未知模板 {tpl_id}，可选: {sorted(REGISTRY)}")
    return REGISTRY[tpl_id]
