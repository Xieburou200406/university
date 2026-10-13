# -*- coding: utf-8 -*-
"""持仓风险度量纯函数（§20.2 P1）：保证金占用估算上界、浮亏触发线、到期日解析。
无 I/O —— 上层 services 负责取数，本模块只做算术与分级（可单测）。

口径与行业依据（《三个交易所期权知识点》§三 / 《期权策略知识点》§十）：
- 上交所 ETF 期权义务仓维持保证金最低标准（每张）：
    [合约前结算价 + Max(12% × 合约前结算价 − 认购虚值, 10% × 标的前收盘价)] × 合约单位
  本实现 **不扣虚值**（账本未存行权价），得到的是 **上界** —— 深度虚值时
  10% × 标的封顶项主导，上界与真实值差距有限；宁可多估不少估（保守原则）。
  组合保证金优惠（买腿覆盖卖腿）同样未建模，上界偏保守方向。
- 浮亏触发线（卖方惯例）：浮亏达应收权利金 50% 预警、100% 强提示。
- 到期周（剩 ≤5 个交易日）：pin risk / 行权指派风险提醒窗口。
"""
import re
from datetime import date

from vol.calendar.exchange_rules import MULTIPLIER_SSE_ETF, fourth_wednesday

UNIT = MULTIPLIER_SSE_ETF          # 10000，合约单位
MARGIN_RATE = 0.12                 # 12% × 合约前结算价
MARGIN_FLOOR = 0.10                # 10% × 标的前收盘价（封顶项）
LOSS_WARN = 0.50                   # 浮亏 / 应收权利金 ≥ 50% → WARN
LOSS_STRONG = 1.00                 # ≥ 100% → STRONG（强提示）
EXPIRY_WEEK_DAYS = 5               # 剩 ≤5 个交易日 → 到期周


def estimate_margin_short(px: float, spot: float, unit: int = UNIT) -> float:
    """单张义务仓维持保证金 **上界**（不扣虚值，见模块注释）。px=合约价，spot=标的价格。"""
    return (px + max(MARGIN_RATE * px, MARGIN_FLOOR * spot)) * unit


def unrealized_pnl(mv: float, qty: int, open_px: float, unit: int = UNIT) -> float:
    """持仓浮动盈亏（元）。约定：开仓现金流 = -qty·open_px·unit（买付卖收），
    平仓得 mv（空头 mv 为负债、负值）→ pnl = mv − qty·open_px·unit。"""
    return mv - qty * open_px * unit


def loss_level(mv: float, qty: int, open_px: float, unit: int = UNIT) -> tuple[float, str | None]:
    """浮亏分级：返回 (亏损比例, 等级)。等级 None / WARN(≥50%) / STRONG(≥100%)。
    盈利仓比例按 0 处理（只警示亏损，不庆祝盈利）。"""
    premium = abs(qty) * open_px * unit          # 应收/应付权利金（开仓基线）
    if premium <= 0:
        return 0.0, None
    ratio = max(0.0, -unrealized_pnl(mv, qty, open_px, unit) / premium)
    if ratio >= LOSS_STRONG:
        return ratio, "STRONG"
    if ratio >= LOSS_WARN:
        return ratio, "WARN"
    return ratio, None


def parse_expiry(code: str) -> date | None:
    """从持仓 code 解析到期日。支持两种格式：
    ① 真实合约码 '510050C2610...' → 到期月第 4 个周三（委托规则库）；
    ② demo 账本 'STRADDLE K=3.0 2026-10-28' → 尾部 ISO 日期。
    解析不出返回 None（fail-soft：风险评审跳过到期项，不崩）。"""
    m = re.match(r"510050[CP](\d{2})(\d{2})", str(code))
    if m:
        return fourth_wednesday(2000 + int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*$", str(code))
    if m:
        try:
            return date.fromisoformat(m.group(1))
        except ValueError:
            return None
    return None
