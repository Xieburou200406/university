# -*- coding: utf-8 -*-
"""持仓风险度量纯函数（§20.2 P1）：保证金占用估算上界、浮亏触发线、到期日解析。
无 I/O —— 上层 services 负责取数，本模块只做算术与分级（可单测）。

口径与行业依据（《三个交易所期权知识点》§三 / 《期权策略知识点》§十）：
- 上交所 ETF 期权义务仓维持保证金最低标准（每张）：
    [合约前结算价 + Max(12% × 合约前结算价 − 认购虚值, 10% × 标的前收盘价)] × 合约单位
  本实现 **不扣虚值**（账本未存行权价），得到的是 **上界** —— 深度虚值时
  10% × 标的封顶项主导，上界与真实值差距有限；宁可多估不少估（保守原则）。
  组合保证金优惠已建模（estimate_portfolio_margin）：同 (类型, 到期月) 内
  买腿覆盖卖腿的价差组合按行权价差计收（见该函数注释）。
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


# ---------------------------------------------------------------- 组合保证金（backlog：买腿覆盖卖腿）
# 口径（保守上界，非交易所逐条规则复刻）：
#   同 (cp, 到期日) 内，一张买腿可覆盖一张同类型卖腿 —— 覆盖成立条件：
#     Call: 行权价(买) ≤ 行权价(卖)（认购牛市价差：买低卖高，损失封顶于价差）；
#     Put : 行权价(买) ≥ 行权价(卖)（认沽熊市价差：买高卖低，同理由价差封顶）。
#   覆盖腿的保证金按 **行权价差 × 合约单位** 计收（最大亏损上界，不再叠加 12%/10% 公式）；
#   行权价相等（完美对冲）记 0。未覆盖卖腿维持单腿上界。买腿自身不占保证金。
#   长腿按张数入池、一张只覆盖一张；demo 账本码（STRADDLE 等）解析失败 → 不参与
#   组合抵扣（fail-soft），维持单腿口径 —— 抵扣只少估保证金，方向仍是保守的。

def estimate_portfolio_margin(legs, unit: int = UNIT) -> dict:
    """组合保证金估算。legs: iterable of (code, qty, cur_px, spot)。
    返回 {"total", "standalone_total", "relief", "covered_contracts", "pairs"}——
    total ≤ standalone_total（差值 relief 即抵扣额）；pairs 为组合明细（trace 用）。
    纯函数无 I/O：解析失败 / qty≥0 腿自动降级为单腿口径，绝不抛出。"""
    from vol.contracts import parse_contract_code

    total = 0.0
    standalone_total = 0.0
    covered_contracts = 0
    pairs: list[dict] = []
    long_pool: dict[tuple, dict[float, int]] = {}

    parsed = []
    for code, qty, cur_px, spot in legs:
        qty = int(qty)
        try:
            spec = parse_contract_code(str(code))
        except ValueError:
            spec = None
        if qty < 0:
            m = estimate_margin_short(float(cur_px), float(spot), unit) * abs(qty)
            total += m
            standalone_total += m
        elif spec is not None:
            key = (spec.cp, spec.expiry)
            pool = long_pool.setdefault(key, {})
            pool[spec.strike] = pool.get(spec.strike, 0) + qty
        parsed.append((code, qty, spec, cur_px, spot))

    for code, qty, spec, cur_px, spot in parsed:
        if qty >= 0 or spec is None:
            continue
        key = (spec.cp, spec.expiry)
        pool = long_pool.get(key)
        if not pool:
            continue
        remain = abs(qty)
        while remain > 0 and pool:
            if spec.cp == "C":
                cand = [k for k in pool if k <= spec.strike]
            else:                                   # Put：买高卖低才被覆盖
                cand = [k for k in pool if k >= spec.strike]
            if not cand:
                break
            k_l = min(cand, key=lambda k: abs(k - spec.strike))   # 最近覆盖（抵扣最小=最保守）
            diff = abs(spec.strike - k_l)
            pool[k_l] -= 1
            if pool[k_l] == 0:
                del pool[k_l]
            covered_contracts += 1
            remain -= 1
            singles = estimate_margin_short(float(cur_px), float(spot), unit)
            charge = min(diff * unit, singles)      # 抵扣上限=原上界（价差过大时宁可不多抵）
            total -= singles - charge               # 该张从全额上界改为按价差计收
            pairs.append({"code": str(code), "covered_by": k_l,
                          "strike_diff": round(diff, 4),
                          "charge": round(charge, 2),
                          "full_margin": round(singles, 2)})

    return {"total": round(total, 2),
            "standalone_total": round(standalone_total, 2),
            "relief": round(standalone_total - total, 2),
            "covered_contracts": covered_contracts,
            "pairs": pairs}


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


def kind_from_code(code: str) -> str | None:
    """从合约码识别期权类型：真实码 '510050C2610...' → 'C'/'P'；
    demo 账本码（STRADDLE 等）无类型 → None（涨跌停走并集宽口径）。"""
    m = re.match(r"510050([CP])", str(code))
    return m.group(1) if m else None


# ---------------------------------------------------------------- §20.2 #5 涨跌停
# 上交所 ETF 期权每日价格涨跌停（合约公开规格；参考知识库仅载机制，公式自上交所细则）：
#   认购：涨幅 = Max{0.5%×前结算, Min[10%×(2×前结算−标的前收), 10%×标的前收]}；跌幅 = 10%×前结算
#   认沽：涨幅 = Max{0.5%×前结算, Min[10%×(2×标的前收−前结算), 10%×标的前收]}；跌幅 = 10%×前结算
# kind=None（跨式/未知类型）：取认购买卖区间与认沽区间的 **并集（更宽者）** ——
#   合成定价的跨式账本腿价按同一数值存储，真实市场里深虚值认购腿日涨幅被压在 0.5% 地板，
#   交集口径会让合成数据天天"越界"（警报疲劳）；单腿真实合约（kind=C/P）走精确口径。

def price_limit_band(settle_px: float, spot_prev: float,
                     kind: str | None = "C") -> tuple[float, float] | None:
    """返回 (跌停价, 涨停价)。settle_px=合约前结算价，spot_prev=标的前收盘价。
    入参非正 → None（fail-soft）。注意：这是 **价格** 限制（期权自身有涨跌停板，
    非标的±10% 的粗略推算——深度虚值腿的上界可能远窄于 10%）。"""
    if settle_px <= 0 or spot_prev <= 0:
        return None
    lower = settle_px * 0.90

    if kind == "C":
        upper = settle_px + max(0.005 * settle_px,
                                min(0.10 * (2 * settle_px - spot_prev), 0.10 * spot_prev))
    elif kind == "P":
        upper = settle_px + max(0.005 * settle_px,
                                min(0.10 * (2 * spot_prev - settle_px), 0.10 * spot_prev))
    else:                                       # None：并集（更宽者，见模块注释）
        up_c = settle_px + max(0.005 * settle_px,
                               min(0.10 * (2 * settle_px - spot_prev), 0.10 * spot_prev))
        up_p = settle_px + max(0.005 * settle_px,
                               min(0.10 * (2 * spot_prev - settle_px), 0.10 * spot_prev))
        upper = max(up_c, up_p)
    return lower, upper


# ---------------------------------------------------------------- §17.2 快照差分归因（唯一实现）
# 铁律：累计快照只能差分，禁止把"刷新值"当"盈亏值"直接加总（对齐 QS 两条铁律之一）。
# panel/面板此前各自内联 diff().fillna(0).cumsum()——收敛到这里成为单一来源。

def pnl_daily_attribution(mvs) -> tuple[list[float], list[float]]:
    """快照差分归因：返回 (逐日盈亏, 累计盈亏)。
    逐日 = mv.diff()（首日无前值记 0）；累计 = 逐日 cumsum。
    数学闭合：累计[-1] == mv[-1] - mv[0]（伸缩恒等式，±1 tick 结案标准的机器表达）。"""
    mvs = [float(v) for v in mvs]
    if not mvs:
        return [], []
    daily = [0.0] + [mvs[i] - mvs[i - 1] for i in range(1, len(mvs))]
    cum: list[float] = []
    acc = 0.0
    for v in daily:
        acc += v
        cum.append(acc)
    return daily, cum
