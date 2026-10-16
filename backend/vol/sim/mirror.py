# -*- coding: utf-8 -*-
"""自动镜像（§12.2/§12.3 auto_mirror 账户，M6-②）。

跟随建议账户：每张建议卡自动按"稳健档"转成 sim 挂单，与自由账户同场竞技。
映射与稳健档口径（保守优先）：
- LONG_VOL  → 买入跨式（ATM），权利金占用 ≤ 账户现金 20%；
- SHORT_VOL → 卖出宽跨式（虚值 10%），两张义务仓保证金上界占用 ≤ 账户现金 20%
  （estimate_margin_short，不扣虚值的保守上界）；
- NEUTRAL / WAIT / 其他 → 不镜像（观望也是建议）；
- verdict == REJECT → 不镜像（建议本身说"已过期，须重新生成"）；
- verdict == WARN  → 仍然镜像：卡面已带警示文本，模拟盘是独立练手场。
去重：同一 (账户, signal_id) 只镜像一次（params_json.signal_id，重跑管线/重复生成不重复建仓）。
到期：≥ 14 个自然日的最近"第四个星期三"月度合约（避开到期周，§20.2 行业惯例）。
镜像失败绝不影响建议卡本身（日志留痕，异常吞掉——镜像锦上添花，不是业务路径）。
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import (AdviceCard, DailyMetric, Signal, SimAccount as SimAccountDB,
                       SimOrder as SimOrderDB)
from vol.calendar.exchange_rules import fourth_wednesday
from vol.position_risk import estimate_margin_short
from vol.sim.broker import UNIT, option_px
from vol.sim.templates import get_template, round_strike

MIRROR_BUDGET_FRAC = 0.2     # 稳健档：占用 ≤ 账户现金 20%
MIRROR_QTY_CAP = 20          # 单笔张数上限（防小权利金撑爆张数）
MIN_DAYS_TO_EXPIRY = 14      # 到期日距下单日下限（自然日，避开到期周）


def _safe_log(code: str, key: str, fields: dict | None = None) -> None:
    """与 api.services._safe_log 同口径；此处独立实现避免循环导入。"""
    try:
        from vol import opslog
        opslog.log_event(code, key, fields)
    except Exception:                               # noqa: BLE001
        pass


def mirror_expiry(d: date, min_days: int = MIN_DAYS_TO_EXPIRY) -> date:
    """≥ min_days 自然日之后的最近月度到期日（第四个星期三，铁律 3：expiry≠真实日期由日历函数保证）。"""
    probe = d.toordinal() + min_days
    y, m = d.year, d.month
    while True:
        wed = fourth_wednesday(y, m)
        if wed.toordinal() >= probe:
            return wed
        m += 1
        if m > 12:
            y, m = y + 1, 1


def _size_long_straddle(spot: float, iv: float, expiry: date, d: date, cash0: float) -> tuple[float, int]:
    """买入跨式稳健档：两张权利金合计占用 ≤ cash0·20%。返回 (单组权利金, 张数)。"""
    T = max((expiry - d).days, 1) / 365.0
    K = round_strike(spot)
    prem = (option_px(spot, K, T, iv, "C") + option_px(spot, K, T, iv, "P")) * UNIT
    budget = cash0 * MIRROR_BUDGET_FRAC
    qty = max(1, min(int(budget // prem) if prem > 0 else 1, MIRROR_QTY_CAP))
    return prem, qty


def _size_short_strangle(spot: float, iv: float, expiry: date, d: date, cash0: float) -> tuple[float, int]:
    """卖出宽跨式稳健档：两张义务仓保证金上界合计占用 ≤ cash0·20%。返回 (单组保证金上界, 张数)。"""
    T = max((expiry - d).days, 1) / 365.0
    k_c, k_p = round_strike(spot * 1.10), round_strike(spot * 0.90)
    margin = (estimate_margin_short(option_px(spot, k_c, T, iv, "C"), spot)
              + estimate_margin_short(option_px(spot, k_p, T, iv, "P"), spot))
    budget = cash0 * MIRROR_BUDGET_FRAC
    qty = max(1, min(int(budget // margin) if margin > 0 else 1, MIRROR_QTY_CAP))
    return margin, qty


def mirror_advice(session: Session, card: AdviceCard, sig: Signal) -> list[SimOrderDB]:
    """把建议卡镜像到所有 auto_mirror 账户。返回新建挂单列表（可能为空）。
    任何失败只记日志，不影响建议卡。"""
    created: list[SimOrderDB] = []
    try:
        verdict = (card.verdict_json or {}).get("verdict")
        if verdict == "REJECT" or sig.signal not in ("LONG_VOL", "SHORT_VOL"):
            return created
        accounts = (session.execute(select(SimAccountDB)
                                    .where(SimAccountDB.auto_mirror.is_(True),
                                           SimAccountDB.status == "active"))
                    .scalars().all())
        if not accounts:
            return created
        dm = session.execute(select(DailyMetric)
                             .where(DailyMetric.date == sig.date)).scalar_one_or_none()
        if dm is None:      # 信息墙：无截面不下单（fail-closed）
            _safe_log("X", "sim_mirror", {"reason": "no_panel", "date": str(sig.date)})
            return created
        expiry = mirror_expiry(sig.date)

        for acct in accounts:
            # 去重：该账户该信号已镜像过
            dup = session.execute(
                select(SimOrderDB).where(SimOrderDB.account_id == acct.id)).scalars().all()
            if any((o.params_json or {}).get("signal_id") == sig.id for o in dup):
                continue

            if sig.signal == "LONG_VOL":
                _, qty = _size_long_straddle(dm.spot, dm.atm_iv, expiry, sig.date, acct.cash0)
                legs = [l.__dict__ | {"expiry": None} for l in
                        get_template("buy_straddle").generate_legs(dm.spot, expiry, qty)]
            else:
                _, qty = _size_short_strangle(dm.spot, dm.atm_iv, expiry, sig.date, acct.cash0)
                legs = [l.__dict__ | {"expiry": None} for l in
                        get_template("sell_strangle").generate_legs(dm.spot, expiry, qty)]

            o = SimOrderDB(account_id=acct.id, tpl_id=f"auto:{sig.signal.lower()}",
                           params_json={"signal_id": sig.id, "auto_mirror": True},
                           legs_json=legs, submit_date=sig.date, expiry=expiry)
            session.add(o)
            created.append(o)
            _safe_log("P", "sim_mirror", {"card": card.id, "signal": sig.signal,
                                          "account": acct.id, "order": o.id,
                                          "qty": qty, "expiry": str(expiry)})
        session.commit()
    except Exception as e:                          # noqa: BLE001
        session.rollback()
        _safe_log("X", "sim_mirror", {"reason": "error", "detail": str(e)[:120]})
    return created
