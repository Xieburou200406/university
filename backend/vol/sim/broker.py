# -*- coding: utf-8 -*-
"""虚拟撮合（§12.2 PaperBroker，简化版：无订单簿，收盘截面判定）。

成交假设偏乐观（§12.5）：无排队、无部分成交。市价单吃单价差 HALF_SPREAD；
费用 FEE_PER_UNIT 元/张固定。限价单语义：当日收盘判定，触及即成，收盘未成
交自动撤销（与真实期权挂单当日有效习惯一致）。
"""
from dataclasses import dataclass, field
from datetime import date

from vol.pricing.bs import bs_price

UNIT = 10000                    # 合约乘数（铁律：上交所 ETF 期权，单一来源 exchange_rules）
FEE_PER_UNIT = 2.0              # 元/张（经手费 + 佣金简化）
HALF_SPREAD = 0.005             # 市价单半价差 0.5%（乐观口径，§12.5）
R_FREE = 0.02                   # 回放定价无风险利率（BS 输入，非决策参数）


@dataclass
class SimLeg:
    """单腿：cp 'C'/'P'，side 'buy'/'sell'，strike 实际行权价（K=3.0 口径）。
    expiry 由订单继承（try_match 填充）。"""
    cp: str
    strike: float
    side: str
    qty: int
    expiry: date | None = None

    def key(self) -> tuple:
        return (self.cp, round(self.strike, 4))


@dataclass
class SimOrder:
    """多腿订单。legs 由模板展开或用户直填；status: pending/filled/cancelled。"""
    account_id: int
    legs: list[SimLeg]
    submit_date: date
    expiry: date
    price_type: str = "market"          # market / limit
    limit_px: float | None = None       # 限价按"单张权利金"口径（元/份）
    tpl_id: str | None = None
    id: int | None = None
    status: str = "pending"


@dataclass
class SimFill:
    """撮合回报：每腿一条。side=buy 现金流出，sell 流入。"""
    order_id: int | None
    leg: SimLeg
    fill_px: float
    qty: int
    cost: float
    filled_date: date

    def cash_delta(self) -> float:
        signed = self.qty if self.leg.side == "buy" else -self.qty
        return -signed * self.fill_px * UNIT - self.cost


def option_px(spot: float, K: float, T_years: float, sigma: float, cp: str,
              r: float = R_FREE) -> float:
    """BS 单张权利金（元/份）。T 必须 > 0（到期日当天按 1 天下限处理，防 0 除）。"""
    return float(bs_price(spot, K, max(T_years, 1.0 / 365.0), r, sigma, cp=cp))


def unit_cost(qty: int) -> float:
    return FEE_PER_UNIT * abs(qty)


def market_fill_px(px: float, side: str) -> float:
    """市价吃单：买入上抬半价差、卖出下压（乐观：仅一档半价差）。"""
    return px * (1 + HALF_SPREAD) if side == "buy" else px * (1 - HALF_SPREAD)


def try_match(order: SimOrder, virtual_date: date, spot: float, sigma: float,
              r: float = R_FREE) -> list[SimFill] | None:
    """按虚拟日收盘截面撮合。全部腿同日成交（组合单原子性）或整体不成。
    T 按 365 自然日口径（铁律 2）。到期日及以后不再成交（作废）。"""
    if order.submit_date > virtual_date or order.status != "pending":
        return None
    if virtual_date >= order.expiry:
        order.status = "cancelled"
        return None
    T = (order.expiry - virtual_date).days / 365.0
    if order.price_type == "limit" and order.limit_px is not None:
        ref = option_px(spot, order.legs[0].strike, T, sigma, order.legs[0].cp, r)
        if order.legs[0].side == "buy" and order.limit_px < ref * (1 + HALF_SPREAD):
            return None                       # 限价买单价未触卖方报价
        if order.legs[0].side == "sell" and order.limit_px > ref * (1 - HALF_SPREAD):
            return None                       # 限价卖单价未触买方报价
    fills = []
    for lg in order.legs:
        lg.expiry = order.expiry
        px = option_px(spot, lg.strike, T, sigma, lg.cp, r)
        if order.price_type == "limit" and order.limit_px is not None:
            px = order.limit_px / max(len(order.legs), 1)   # 组合限价按腿均摊（简化）
        else:
            px = market_fill_px(px, lg.side)
        fills.append(SimFill(order_id=order.id, leg=lg, fill_px=round(px, 6),
                             qty=abs(lg.qty), cost=unit_cost(lg.qty),
                             filled_date=virtual_date))
    order.status = "filled"
    return fills
