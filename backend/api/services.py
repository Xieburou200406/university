# -*- coding: utf-8 -*-
"""业务服务层：①建议卡片生成（风控链 trace）②每日信号管线（复用 m2/m3 已迁移的领域包）。
铁律 7：任何一步失败即 fail-closed（不出卡 / 管线标记 failed）。铁律 8：T+1 过期作废。"""
import sys
import time
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (AdviceCard, DailyMetric, OpenForecast, PipelineRun,
                       Position, PositionSnapshot, ReviewLog, RiskBudget, Signal)
from db.models import SimAccount as SimAccountDB
from db.models import SimFill as SimFillDB
from db.models import SimNav as SimNavDB
from db.models import SimOrder as SimOrderDB
from db.models import SimSessionRec as SimSessionRecDB
from vol import opslog
from vol import position_risk as pr
from vol.calendar import get_calendar
from vol.config import Settings

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:          # m2/m3 在仓库根
    sys.path.insert(0, str(_ROOT))

_ACTION = {"SHORT_VOL": "倾向卖波（收权利金方向）", "LONG_VOL": "倾向买波（波动率多头方向）",
           "NEUTRAL": "观望", "WAIT": "观望", "INSUFFICIENT_HISTORY": "观望（历史不足）"}


def _safe_log(code: str, key: str, fields: dict | None = None) -> None:
    """M7-① 结构化事件日志（§17.1）。日志失败绝不影响业务——日志是运维眼睛，不是业务路径。"""
    try:
        opslog.log_event(code, key, fields)
    except Exception:                               # noqa: BLE001
        pass


def _exec_window(d: date) -> str:
    """T+1 执行窗口提示（铁律 8）：下一交易日的 9:35~10:00（§18 交易日历）。"""
    return get_calendar().exec_window(d)


def latest_exposure(session: Session) -> tuple[date | None, float, float]:
    """最新快照日的组合 Vega / Delta 敞口（绝对值口径）。"""
    latest = session.execute(select(func.max(PositionSnapshot.date))).scalar_one()
    if latest is None:
        return None, 0.0, 0.0
    rows = session.execute(select(PositionSnapshot).where(PositionSnapshot.date == latest)).scalars().all()
    open_ids = set(session.execute(
        select(Position.id).where(func.upper(Position.status) == "OPEN")).scalars().all())
    vega = sum(r.vega for r in rows if r.position_id in open_ids)
    delta = sum(r.delta for r in rows if r.position_id in open_ids)
    return latest, vega, delta


def snapshot_positions(session: Session, d: date, spot: float, iv: float) -> dict:
    """持仓收盘估值快照（§RiskService.snapshot 落地，管线每日最后一步）。
    对 OPEN 持仓按合约代码 BS 估值（用当日截面 spot/IV），落 position_snapshot。
    口径：delta/vega/theta（元）= 单张 greeks × qty(带符号) × UNIT(10000)，theta 年化。
    幂等：同 (date, position_id) 已存在 → DELETE+INSERT 覆盖（append-only 触发器只拦
    UPDATE，当日行重跑重写、历史行不动，与设计 §管线幂等一致）。
    code 不可解析（演示/遗留持仓）→ fail-soft 跳过并记日志，绝不阻断管线。"""
    from vol.contracts import parse_contract_code
    from vol.pricing.bs import bs_price, greeks
    from vol.sim.broker import R_FREE
    out = {"date": str(d), "snapshotted": 0, "skipped": [], "open_positions": 0}
    opens = session.execute(select(Position)
                            .where(func.upper(Position.status) == "OPEN")).scalars().all()
    out["open_positions"] = len(opens)
    for p in opens:
        try:
            spec = parse_contract_code(p.code)
        except ValueError:
            out["skipped"].append({"id": p.id, "reason": f"code 不可解析: {p.code[:40]}"})
            continue
        T = max((spec.expiry - d).days, 1) / 365.0            # 铁律 2：365 自然日
        dd, _, vv, tt = greeks(spot, spec.strike, T, R_FREE, iv, spec.cp)
        px = bs_price(spot, spec.strike, T, R_FREE, iv, spec.cp)
        row = session.execute(select(PositionSnapshot).where(
            PositionSnapshot.date == d, PositionSnapshot.position_id == p.id)).scalar_one_or_none()
        if row is not None:
            session.delete(row)                               # REPLACE 语义（当日行重写）
            session.flush()
        session.add(PositionSnapshot(
            date=d, position_id=p.id,
            mv=p.qty * px * pr.UNIT,
            delta=p.qty * dd * pr.UNIT, vega=p.qty * vv * pr.UNIT,
            theta=p.qty * tt * pr.UNIT, iv=iv, spot=spot))
        out["snapshotted"] += 1
    session.flush()
    if out["snapshotted"] or out["skipped"]:
        _safe_log("S", "position_snapshot", {"date": str(d).replace("-", ""),
                                             "ok": out["snapshotted"],
                                             "skip": len(out["skipped"])})
    return out


def position_risk_review(session: Session, asof: date | None = None) -> dict:
    """持仓风险评审（§20.2 P1）：保证金占用估算 + 浮亏触发线 + 到期周提醒。
    纯聚合：只读 Position × PositionSnapshot，算术全部委托 vol.position_risk 纯函数。
    快照取 asof（默认最新快照日）；某持仓当日无快照则跳过（fail-soft，不虚构估值）。"""
    cal = get_calendar()
    if asof is None:
        asof = session.execute(select(func.max(PositionSnapshot.date))).scalar_one()
    if asof is None:
        return {"asof": None, "rows": [], "total_margin_est": 0.0,
                "max_loss_level": None, "expiry_week": [], "price_limit_hits": []}

    snaps = {s.position_id: s for s in session.execute(
        select(PositionSnapshot).where(PositionSnapshot.date == asof)).scalars().all()}
    positions = session.execute(
        select(Position).where(func.upper(Position.status) == "OPEN")).scalars().all()

    rows: list[dict] = []
    for p in positions:
        s = snaps.get(p.id)
        if s is None:
            continue
        qty = int(p.qty)
        cur_px = abs(s.mv) / (abs(qty) * pr.UNIT) if qty else 0.0
        ratio, level = pr.loss_level(s.mv, qty, p.open_px)
        margin = pr.estimate_margin_short(cur_px, s.spot) * abs(qty) if qty < 0 else 0.0
        exp = pr.parse_expiry(p.code)
        td_left = cal.countdown(asof, exp) if exp else None
        # §20.2 #5 涨跌停可执行性：前一日快照 px 为"前结算"代理（mark 非 official settle，注明局限）
        limit_hit = None
        if abs(qty) > 0:
            prev2 = session.execute(
                select(PositionSnapshot)
                .where(PositionSnapshot.position_id == p.id, PositionSnapshot.date < asof)
                .order_by(PositionSnapshot.date.desc()).limit(1)).scalar_one_or_none()
            if prev2 is not None:
                prev_px = abs(prev2.mv) / (abs(qty) * pr.UNIT)
                band = pr.price_limit_band(prev_px, prev2.spot, pr.kind_from_code(p.code))
                if band and not (band[0] <= cur_px <= band[1]):
                    limit_hit = "UP" if cur_px > band[1] else "DOWN"
        rows.append({
            "code": p.code, "qty": qty, "open_px": p.open_px, "cur_px": round(cur_px, 4),
            "unrealized_pnl": round(pr.unrealized_pnl(s.mv, qty, p.open_px), 2),
            "loss_ratio": round(ratio, 4), "loss_level": level,
            "margin_est": round(margin, 2),
            "expiry": str(exp) if exp else None,
            "trading_days_left": td_left,
            "expiry_week": td_left is not None and td_left <= pr.EXPIRY_WEEK_DAYS,
            "price_limit_hit": limit_hit,
        })

    levels = [r["loss_level"] for r in rows if r["loss_level"]]
    return {
        "asof": str(asof), "rows": rows,
        "total_margin_est": round(sum(r["margin_est"] for r in rows), 2),
        "max_loss_level": "STRONG" if "STRONG" in levels else ("WARN" if "WARN" in levels else None),
        "expiry_week": [r["code"] for r in rows if r["expiry_week"]],
        "price_limit_hits": [f"{r['code']}({r['price_limit_hit']})" for r in rows if r["price_limit_hit"]],
    }


def fetch_live_quotes(codes: list[str]) -> dict:
    """逐合约实时盘口（新浪，8 线程并行；M7-⑥ 流动性门取数，与 run_demo.step3 同源）。
    单合约失败 → None；整体异常 → {}（调用方 fail-open 并留痕）。绝不抛出、绝不阻断。"""
    try:
        import akshare as ak
        from concurrent.futures import ThreadPoolExecutor

        def _one(code):
            try:
                df = ak.option_sse_spot_price_sina(symbol=str(code))
                m = dict(zip(df["字段"], df["值"]))
                return code, {"bid": float(m.get("买价") or 0), "ask": float(m.get("卖价") or 0),
                              "last": float(m.get("最新价") or 0),
                              "volume": float(m.get("成交量") or 0),
                              "oi": float(m.get("持仓量") or 0)}
            except Exception:
                return code, None

        out: dict = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            for code, q in ex.map(_one, codes):
                out[code] = q
        return out
    except Exception:
        return {}


def generate_advice(session: Session, settings: Settings, signal_id: int) -> AdviceCard:
    """由信号生成建议卡：跑风控条件链，全程留 trace（verdict_json）。只建议，不下单。"""
    sig = session.get(Signal, signal_id)
    if sig is None:
        raise LookupError(f"signal {signal_id} 不存在")

    checks: list[dict] = []
    today = date.today()
    from vol.contracts import parse_contract_code

    # ① 有效期（铁律 8：T+1 收盘信号次日有效，过期作废）
    valid = sig.valid_until is not None and sig.valid_until >= today
    checks.append({"name": "signal_valid_until", "passed": valid,
                   "detail": f"valid_until={sig.valid_until}, today={today}"})

    # ② Vega 限额
    snap_date, vega, delta = latest_exposure(session)
    vega_ok = abs(vega) <= settings.vega_limit
    # 快照新鲜度：快照日落后于面板最新截面 → 敞口是旧数字（管线停跑或持仓未估值）
    panel_latest = session.execute(select(func.max(DailyMetric.date))).scalar_one()
    stale = snap_date is not None and panel_latest is not None and snap_date < panel_latest
    checks.append({"name": "vega_limit", "passed": vega_ok,
                   "detail": (f"组合vega={vega:.1f} (每1.0 IV口径), 限额±{settings.vega_limit}, 快照日={snap_date}"
                              + (f"；⚠ 快照落后面板最新截面 {panel_latest}，敞口非当日口径" if stale else ""))})

    # ③ Delta 限额
    delta_ok = abs(delta) <= settings.delta_limit
    checks.append({"name": "delta_limit", "passed": delta_ok,
                   "detail": f"组合delta={delta:.1f}, 限额±{settings.delta_limit}"})

    # ④ 持仓风险评审（§20.2 P1）：浮亏触发线 + 到期周 + 保证金占用 + 涨跌停（P2）
    review = position_risk_review(session)
    loss_ok = review["max_loss_level"] != "STRONG"
    checks.append({"name": "floating_loss", "passed": loss_ok,
                   "detail": (f"最差浮亏等级={review['max_loss_level'] or '正常'}"
                              f"（阈值：≥50% WARN / ≥100% STRONG，权利金口径）")})
    expiry_week = review["expiry_week"]
    limit_ok = not review["price_limit_hits"]
    checks.append({"name": "price_limit", "passed": limit_ok,
                   "detail": ("相关合约在涨跌停区间内" if limit_ok
                              else f"触及涨跌停区间：{'、'.join(review['price_limit_hits'])}（今日可能无法成交/流动性枯竭）")})

    # ⑥ 同一行权价集中度（SingleStrikeRule）：OPEN 持仓按行权价聚合张数
    opens = session.execute(
        select(Position).where(func.upper(Position.status) == "OPEN")).scalars().all()
    strike_qty: dict = {}
    unparsed_codes = 0
    for p in opens:
        try:
            spec6 = parse_contract_code(p.code)
        except ValueError:
            unparsed_codes += 1
            continue
        strike_qty[spec6.strike] = strike_qty.get(spec6.strike, 0) + abs(int(p.qty))
    if strike_qty:
        worst_strike, worst_qty = max(strike_qty.items(), key=lambda kv: kv[1])
        single_ok = worst_qty <= settings.single_strike_limit
        checks.append({"name": "single_strike", "passed": single_ok,
                       "detail": (f"最大单行权价集中度 {worst_strike:.3f} 元 × {worst_qty} 张"
                                  f"（上限 {settings.single_strike_limit} 张/档）"
                                  + (f"；{unparsed_codes} 个持仓代码不可解析跳过" if unparsed_codes else ""))})
    else:
        single_ok = True
        checks.append({"name": "single_strike", "passed": True,
                       "detail": "无 OPEN 持仓，集中度检查通过"})

    # ⑦ 深值禁加仓（DeepMoneyRule）：最新快照逐腿还原单张 |Δ| ∈ [low, high]
    deep_list: list[str] = []
    if snap_date is not None:
        snap_pairs = session.execute(
            select(PositionSnapshot, Position)
            .join(Position, Position.id == PositionSnapshot.position_id)
            .where(PositionSnapshot.date == snap_date)).all()
        for srow, prow in snap_pairs:
            if (prow.status or "").upper() != "OPEN" or int(prow.qty) == 0:
                continue
            unit_delta = abs(float(srow.delta)) / (abs(int(prow.qty)) * pr.UNIT)
            if unit_delta > settings.deep_money_high or unit_delta < settings.deep_money_low:
                deep_list.append(f"{prow.code}(Δ={unit_delta:.2f})")
    deep_ok = not deep_list
    checks.append({"name": "deep_money", "passed": deep_ok,
                   "detail": ("无持仓快照，深值检查跳过（快照未生成不虚构）" if snap_date is None else
                              (f"持仓单张 |Δ| 全部落在 [{settings.deep_money_low:.2f}, {settings.deep_money_high:.2f}]" if deep_ok else
                               f"深虚/深实持仓（只平不开）：{'、'.join(deep_list[:5])}"
                               + (f" 等 {len(deep_list)} 个" if len(deep_list) > 5 else "")))})

    # ⑧ 资金占用（CapitalUsageRule）：保证金占用估算 / 基准资金
    margin_total = float(review.get("total_margin_est") or 0.0)
    usage = margin_total / settings.capital_base if settings.capital_base > 0 else 0.0
    capital_ok = usage <= settings.capital_usage_limit
    checks.append({"name": "capital_usage", "passed": capital_ok,
                   "detail": (f"保证金占用估算 {margin_total:.0f} 元 / 基准资金 {settings.capital_base:.0f} 元"
                              f" = {usage:.1%}（上限 {settings.capital_usage_limit:.0%}）")})

    # ⑨ 流动性门（LiquidityGateRule）：OPEN 持仓实时盘口 → 0-100 分（价差 60% + 量分位 40%）
    liq_codes: list[str] = []
    for p in opens:
        try:
            parse_contract_code(p.code)
            liq_codes.append(p.code)
        except ValueError:
            pass
    if not liq_codes:
        liq_ok = True
        checks.append({"name": "liquidity_gate", "passed": True,
                       "detail": "无可解析持仓合约，盘口流动性检查跳过"})
    else:
        quotes = fetch_live_quotes(liq_codes)
        valid_quotes = {c: q for c, q in quotes.items() if q}
        if not valid_quotes:
            liq_ok = True
            checks.append({"name": "liquidity_gate", "passed": True,
                           "detail": f"实时盘口不可用（{len(liq_codes)} 个合约），fail-open 跳过并留痕"})
        else:
            from vol.pricing.liquidity import liquidity_score
            vlist = [float(q.get("volume") or 0) for q in valid_quotes.values()]
            olist = [float(q.get("oi") or 0) for q in valid_quotes.values()]

            def _pct_rank(val, arr):
                return (sum(1 for x in arr if x <= val) / len(arr)) if arr else 1.0

            low_liq: list[str] = []
            scored_n = 0
            for c, q in valid_quotes.items():
                sc = liquidity_score(float(q.get("bid") or 0), float(q.get("ask") or 0),
                                     _pct_rank(float(q.get("volume") or 0), vlist),
                                     _pct_rank(float(q.get("oi") or 0), olist))
                scored_n += 1
                if sc < settings.liq_min_score:
                    low_liq.append(f"{c}({sc:.0f}分)")
            liq_ok = not low_liq
            checks.append({"name": "liquidity_gate", "passed": liq_ok,
                           "detail": (f"盘口流动性 {scored_n}/{len(quotes)} 个有效，全部 ≥ {settings.liq_min_score} 分" if liq_ok else
                                      f"低于 {settings.liq_min_score} 分的持仓：{'、'.join(low_liq)}"
                                      f"（执行前核对盘口，优先换主力月份载体）")})

    # ⑩ 信号一致性（SignalConsistencyRule）：建议由当日信号生成，方向同源
    checks.append({"name": "signal_consistency", "passed": True,
                   "detail": f"建议由 signal#{sig.id}（{sig.engine}/{sig.signal}）生成，方向同源一致"})

    hard_fail = not valid
    soft_warn = not (vega_ok and delta_ok and loss_ok and limit_ok
                     and single_ok and deep_ok and capital_ok and liq_ok)
    verdict = "REJECT" if hard_fail else ("WARN" if soft_warn else "PASS")

    action = _ACTION.get(sig.signal, "观望")
    if isinstance(sig.detail_json, dict) and sig.detail_json.get("confirm_needed", 0) > 0:
        action += f"（状态翻转待确认，还需 {sig.detail_json['confirm_needed']} 日）"
    if verdict == "REJECT":
        action = "已过期，执行前必须基于最新信号重新生成建议"
    if verdict == "WARN":
        action += "；⚠️ 组合敞口接近/超出限额，先减仓再考虑新头寸"
    if not loss_ok:
        action += "；🚨 现有持仓浮亏已达权利金 100%，行业惯例优先处理亏损头寸而非加新仓"
    if not single_ok:
        action += "；🎯 同一行权价持仓集中度超限——先分散或减仓再考虑新头寸"
    if not deep_ok:
        action += "；🕳️ 持仓含深虚/深实腿（|Δ| 越界）——行业惯例只平不开"
    if not capital_ok:
        action += "；💰 保证金占用超基准资金上限——先释放资金再考虑新头寸"
    if not liq_ok:
        action += "；🚧 部分持仓流动性分过低——执行前核对盘口，优先换主力月份载体"
    if expiry_week:
        action += f"；⏳ 到期周提醒（剩≤5个交易日：{'、'.join(expiry_week)}）——注意 pin risk 与行权指派，行业惯例提前移仓"
    if not limit_ok:
        action += "；🚧 相关合约触及涨跌停区间——今日可能挂不出去或流动性枯竭，执行窗口内先核对盘口再动手"

    card = {
        "date": str(sig.date), "signal": sig.signal, "action": action,
        "engine": sig.engine, "confidence": sig.confidence,
        "valid_until": str(sig.valid_until) if sig.valid_until else None,
        "exec_window": _exec_window(sig.date),
        "position_risk": review,
        "disclaimer": "本系统只生成建议，不自动下单；执行前必须用 T+1 最新行情复核风控条件链",
    }
    ac = AdviceCard(signal_id=sig.id, verdict_json={"verdict": verdict, "checks": checks},
                    card_json=card)
    session.add(ac)
    session.flush()
    _safe_log("A", "advice", {"card": ac.id, "verdict": verdict, "signal": sig.signal,
                              "valid_until": str(sig.valid_until)})

    # M6-②：跟随建议账户自动镜像（稳健档挂单），结果写回卡面（失败只记日志不影响卡）
    from vol.sim.mirror import mirror_advice
    mirrored = mirror_advice(session, ac, sig)
    if mirrored:
        card["auto_mirror"] = {"orders": [{"account_id": o.account_id, "order_id": o.id,
                                           "qty": o.legs_json[0].get("qty"),
                                           "expiry": str(o.expiry)} for o in mirrored]}
        ac.card_json = card
    return ac


# ---------------------------------------------------------------- 管线
def compute_and_store_daily(session: Session, settings: Settings, fetch_df=None) -> dict:
    """每日信号管线：拉面板 → 百分位引擎 + 类比引擎 → 落 daily_metric + signal。
    fetch_df 可注入（测试用）；默认走 m2 缓存/联网。返回摘要。"""
    if fetch_df is None:
        from m2_run import build_daily_history
        df = build_daily_history()
    else:
        df = fetch_df
    if df is None or len(df) < 2:
        _safe_log("X", "pipeline", {"reason": "no_panel", "detail": "面板数据不可用（fail-closed）"})
        raise RuntimeError("面板数据不可用（fail-closed）")
    t0 = time.perf_counter()
    _safe_log("D", "panel", {"date": str(df["date"].iloc[-1]).replace("-", ""),
                             "rows": len(df), "src": "injected" if fetch_df is not None else "akshare"})

    from vol.signals import PercentileEngine
    eng = PercentileEngine(window=settings.window, confirm_days=settings.confirm_days)
    pct = eng.run(df["iv_near"].tolist())

    from m3_run import analog_states
    ana_last = analog_states(df, k=settings.k_nearest, horizon=settings.horizon)[-1]

    d_str = str(df["date"].iloc[-1])
    d = date.fromisoformat(d_str)
    iv_now = float(df["iv_near"].iloc[-1])
    pct40 = float((df["iv_near"].tail(40) <= iv_now).mean())

    # daily_metric upsert（按主键 date）
    dm = session.get(DailyMetric, d)
    if dm is None:
        dm = DailyMetric(date=d)
        session.add(dm)
    dm.spot = float(df["spot"].iloc[-1])
    dm.atm_iv = iv_now
    dm.iv_pct_40 = round(pct40, 4)
    dm.iv_pct_120 = pct.get("iv_pct")
    dm.skew_25d = None if df["skew25"].iloc[-1] is None else float(df["skew25"].iloc[-1])
    dm.momentum_20d = float(df["spot"].iloc[-1] / df["spot"].iloc[-21] - 1.0) if len(df) > 20 else None

    # 信号落库（百分位 + 类比各自一条；ensemble 结论并入 detail）
    rows = [Signal(date=d, engine="percentile", signal=pct["signal"],
                   confidence=None, valid_until=get_calendar().next_trading_day(d), detail_json=pct)]
    ana_sig = {1: "LONG_VOL", -1: "SHORT_VOL", 0: "NEUTRAL"}[int(ana_last)]
    rows.append(Signal(date=d, engine="analog", signal=ana_sig, confidence=None,
                       valid_until=get_calendar().next_trading_day(d),
                       detail_json={"k": settings.k_nearest, "horizon": settings.horizon}))
    agree = (pct["signal"] == ana_sig and pct["signal"] in ("LONG_VOL", "SHORT_VOL"))
    rows.append(Signal(date=d, engine="ensemble",
                       signal=pct["signal"] if pct["signal"] in ("LONG_VOL", "SHORT_VOL", "NEUTRAL") else "NEUTRAL",
                       confidence=0.75 if agree else 0.4,
                       valid_until=get_calendar().next_trading_day(d),
                       detail_json={"analog": ana_sig, "agreed": agree,
                                    "note": "同向增强；不一致则降级参考"}))
    session.add_all(rows)
    session.flush()
    for r in rows:                                   # M7-①：明细级 S| 每条信号一行（负记录：NEUTRAL 也记）
        _safe_log("S", "signal", {"engine": r.engine, "verdict": r.signal,
                                  "date": d_str.replace("-", ""), "valid_until": str(r.valid_until)})
    _safe_log("P", "pipeline", {"step": "compute_daily", "ok": 1,
                                "ms": int((time.perf_counter() - t0) * 1000)})
    # 持仓收盘估值快照（§RiskService.snapshot 落地）：跟着管线走，vega/delta 限额才不卡死
    snap = snapshot_positions(session, d, float(df["spot"].iloc[-1]), iv_now)
    jump = _iv_jump_review(session, d, {})          # §17.6：跳变留痕 + 连续跳变降级
    return {"date": d_str, "percentile": pct["signal"], "analog": ana_sig,
            "agree": agree, "iv_now": round(iv_now, 4), "iv_pct_120": pct.get("iv_pct"),
            "iv_jump": jump["iv_jump"], "iv_degrade": jump["iv_degrade"],
            "snapshot": snap}


LAMBDA_MAP = {"conservative": 8.0, "balanced": 4.0, "aggressive": 2.0}   # §14.1 三档

# §17.6 跳变留痕与保护性降级：iv_pct_120 单日变动 > 0.30（分位口径 0~1）→ 跳变；
# 连续 2 个跳变日 → 信号链降级标记（首页角标，不硬给结论）。
JUMP_THRESHOLD = 0.30


def _iv_jump_review(session: Session, d: date, summary: dict) -> dict:
    """IV 跳变审查（§17.6）：比对最近 3 个 DailyMetric 的 iv_pct_120。
    单日跳变 → X|jump 留痕（当日原始数据 = DailyMetric 行本身，落库即留痕）；
    连续 2 个跳变日 → X|degrade + summary 标记 iv_degrade=True（信号链降级，与 §16.4 衔接）。"""
    rows = session.execute(
        select(DailyMetric).where(DailyMetric.date <= d)
        .order_by(DailyMetric.date.desc()).limit(3)).scalars().all()
    vals = [(r.date, r.iv_pct_120) for r in rows]           # [今日, 昨日, 前日]

    def _jump(a, b) -> bool:
        return a is not None and b is not None and abs(a - b) > JUMP_THRESHOLD

    jump_today = len(vals) >= 2 and _jump(vals[0][1], vals[1][1])
    out = {"iv_jump": jump_today, "iv_degrade": False}
    if jump_today:
        delta = round(vals[0][1] - vals[1][1], 4)
        _safe_log("X", "jump", {"date": str(vals[0][0]).replace("-", ""),
                                "from": vals[1][1], "to": vals[0][1], "delta": delta,
                                "note": "IV 单日跳变>30分位；原始数据见 daily_metric 当日行"})
        out["iv_delta"] = delta
        if len(vals) >= 3 and _jump(vals[1][1], vals[2][1]):
            out["iv_degrade"] = True
            _safe_log("X", "degrade", {"date": str(vals[0][0]).replace("-", ""),
                                       "note": "连续2日IV跳变 → 信号链降级标记，结论仅作参考"})
    return out


# ---- §18.6 建议生命周期：滞留-采纳闭环 ----
# T 收盘后生成 pending 卡（语义：给 T+1）→ T+1 开盘前打开面板即"今日建议"再呈现
# → 有效期内一直挂到 采纳/忽略，过期未处理由滞留清算自动标 expired（留痕不删）。

def expire_stale_advice(session: Session, today: date) -> int:
    """滞留清算：pending 卡对应的信号已过期（valid_until < today）→ 标 expired。留痕不删。"""
    rows = session.execute(
        select(AdviceCard).join(Signal, AdviceCard.signal_id == Signal.id)
        .where(AdviceCard.status == "pending", Signal.valid_until < today)
    ).scalars().all()
    for c in rows:
        c.status = "expired"
    if rows:
        session.commit()
    return len(rows)


def todays_advice(session: Session, today: date) -> AdviceCard | None:
    """今日待采纳卡：最新一张 pending 且信号仍在有效期（滞留再呈现，直到采纳/忽略/过期）。"""
    expire_stale_advice(session, today)
    return session.execute(
        select(AdviceCard).join(Signal, AdviceCard.signal_id == Signal.id)
        .where(AdviceCard.status == "pending", Signal.valid_until >= today)
        .order_by(AdviceCard.id.desc()).limit(1)
    ).scalar_one_or_none()


def adopt_advice(session: Session, card_id: int, today: date | None = None) -> AdviceCard:
    """采纳建议（留痕：几点采纳）。过期卡不可采纳（铁律 8）。"""
    ac = session.get(AdviceCard, card_id)
    if ac is None:
        raise LookupError(f"advice_card {card_id} 不存在")
    if ac.status != "pending":
        raise ValueError(f"卡片状态为 {ac.status}，不可采纳")
    sig = session.get(Signal, ac.signal_id)
    d = today or date.today()
    if sig is None or sig.valid_until is None or sig.valid_until < d:
        raise ValueError("信号已过期，不可采纳（T+1 有效，铁律 8）")
    ac.status, ac.adopted_at = "adopted", datetime.now()
    session.commit()
    return ac


def dismiss_advice(session: Session, card_id: int) -> AdviceCard:
    """忽略建议（用户主动放弃本次参考）。"""
    ac = session.get(AdviceCard, card_id)
    if ac is None:
        raise LookupError(f"advice_card {card_id} 不存在")
    if ac.status != "pending":
        raise ValueError(f"卡片状态为 {ac.status}，不可忽略")
    ac.status = "dismissed"
    session.commit()
    return ac


def _derive_forecast_features(df):
    """从 m2 面板派生预测特征列（§13.2）。若面板已带完整特征列则原样返回。"""
    import numpy as np
    missing = [c for c in ("atm_iv", "iv_pct_120", "term_slope", "vrp", "momentum_20d") if c not in df.columns]
    if not missing:
        return df
    df = df.copy()
    df["atm_iv"] = df["iv_near"]
    df["iv_pct_120"] = df["iv_near"].rolling(120).apply(lambda s: float((s <= s.iloc[-1]).mean()), raw=False)
    df["term_slope"] = df["iv_near"] - df["iv_next"].fillna(df["iv_near"])
    df["vrp"] = df["iv_near"] - df["rvol20"] if "rvol20" in df.columns else 0.0
    df["momentum_20d"] = df["spot"].pct_change(20) if "spot" in df.columns else 0.0
    df["skew25"] = df["skew25"].fillna(0.0) if "skew25" in df.columns else 0.0
    return df


def compute_and_store_forecast(session: Session, settings: Settings, df,
                               open_map: dict | None = None) -> OpenForecast:
    """§13 管线步：隔夜开盘方向预测 → upsert open_forecast。任何失败落 abstain 行（fail-closed）。"""
    d = date.fromisoformat(str(df["date"].iloc[-1]))
    try:
        import numpy as np
        if open_map:
            df = df.assign(open=df["date"].map(open_map).astype(float))
        else:
            df = df.assign(open=np.nan)
        df = _derive_forecast_features(df)
        from vol.prediction.forecast import OpenDirectionForecaster
        fc = OpenDirectionForecaster().forecast(df)
        row = session.get(OpenForecast, d)
        if row is None:
            row = OpenForecast(date=d)
            session.add(row)
        row.model_ver, row.p_up_raw, row.p_up_cal = "lr-irls-v1", fc.p_up_raw, fc.p_up_cal
        row.abstain, row.reason, row.features_json = fc.abstain, fc.reason, fc.coef
        session.flush()
        # 负记录原则（§17.1）：弃权也落明文，日志必须能回答"为什么没给预测"
        _safe_log("F" if not fc.abstain else "X", "forecast",
                  {"date": d.strftime("%Y%m%d"), "p_up": round(fc.p_up_cal, 4) if fc.p_up_cal else None,
                   "reason": fc.reason})
        return row
    except Exception as e:                          # noqa: BLE001 —— 预测失败不拖垮管线
        row = session.get(OpenForecast, d)
        if row is None:
            row = OpenForecast(date=d)
            session.add(row)
        row.abstain, row.reason = True, f"预测管线异常（fail-closed）: {type(e).__name__}: {e}"[:500]
        session.flush()
        _safe_log("X", "forecast", {"reason": "pipeline_error", "detail": row.reason[:120]})
        return row


def compute_and_store_budget(session: Session, settings: Settings, df,
                             lambda_level: str = "balanced") -> RiskBudget:
    """§14 管线步：今日风险预算 → upsert risk_budget。失败落降级行（维持现仓位）。"""
    d = date.fromisoformat(str(df["date"].iloc[-1]))
    try:
        from vol.riskbudget.optimize import solve_budget
        from vol.riskbudget.scenarios import build_scenarios
        pack = build_scenarios(df, k=settings.k_nearest, horizon=settings.horizon,
                               window=settings.window)
        _, cur_vega, cur_delta = latest_exposure(session)
        theta_annual = _open_theta_annual(session)
        res = solve_budget(cur_vega, cur_delta, theta_annual, pack,
                           LAMBDA_MAP.get(lambda_level, 4.0),
                           settings.vega_limit, settings.delta_limit,
                           lambda_level, spot=float(df["spot"].iloc[-1]))
        width = 0.25 * abs(res.target_vega) if not res.degraded else 0.0
        width_d = 0.25 * abs(res.target_delta) if not res.degraded else 0.0
        row = session.get(RiskBudget, d)
        if row is None:
            row = RiskBudget(date=d)
            session.add(row)
        row.lambda_level = lambda_level
        row.target_vega, row.target_vega_lo, row.target_vega_hi = res.target_vega, res.target_vega - width, res.target_vega + width
        row.target_delta, row.target_delta_lo, row.target_delta_hi = res.target_delta, res.target_delta - width_d, res.target_delta + width_d
        row.cur_vega, row.cur_delta, row.cvar5 = cur_vega, cur_delta, res.cvar5
        row.confidence, row.degraded = res.confidence, res.degraded
        row.notes_json = {"reason": res.reason, "checks": res.checks, "state": [res.vega_state, res.delta_state]}
        session.flush()
        _safe_log("B", "budget", {"lambda": lambda_level, "target_vega": round(res.target_vega, 3),
                                  "conf": res.confidence, "degraded": int(res.degraded)})
        return row
    except Exception as e:                          # noqa: BLE001 —— 预算失败落降级行
        _, cur_vega, cur_delta = latest_exposure(session)
        row = session.get(RiskBudget, d)
        if row is None:
            row = RiskBudget(date=d)
            session.add(row)
        row.lambda_level = lambda_level
        row.target_vega = row.target_vega_lo = row.target_vega_hi = cur_vega
        row.target_delta = row.target_delta_lo = row.target_delta_hi = cur_delta
        row.cur_vega, row.cur_delta, row.cvar5 = cur_vega, cur_delta, None
        row.confidence, row.degraded = "LOW", True
        row.notes_json = {"reason": f"预算管线异常（fail-closed）: {type(e).__name__}: {e}"[:500]}
        session.flush()
        _safe_log("X", "budget", {"reason": "pipeline_error", "detail": row.notes_json["reason"][:120]})
        return row


def _open_theta_annual(session: Session) -> float:
    """最新快照日 open 持仓的年化 Theta（正式库 theta 列已是年化口径，铁律 2）。"""
    latest = session.execute(select(func.max(PositionSnapshot.date))).scalar_one()
    if latest is None:
        return 0.0
    open_ids = set(session.execute(
        select(Position.id).where(func.upper(Position.status) == "OPEN")).scalars().all())
    rows = session.execute(select(PositionSnapshot).where(PositionSnapshot.date == latest)).scalars().all()
    return float(sum(r.theta for r in rows if r.position_id in open_ids))


def _fetch_open_map(days: int = 30) -> dict | None:
    """T+1 开盘价映射（§13 标签/晨检复核）。委托 m2_run.fetch_open_map（M8-① 起单一来源）。
    联网失败返回 None → 预测自然弃权（fail-closed）。"""
    from m2_run import fetch_open_map
    return fetch_open_map(days)


def trigger_pipeline(session: Session, fetch_df=None) -> PipelineRun:
    """防重入锁：存在 running 记录即拒绝（铁律 9：单一入口）。失败 fail-closed 落 failed 记录。"""
    running = session.execute(select(PipelineRun).where(PipelineRun.status == "running")
                              .limit(1)).scalar_one_or_none()
    if running is not None:
        _safe_log("X", "pipeline", {"reason": "reentry", "detail": f"run={running.id} 仍在运行"})
        raise PermissionError(f"已有管线在运行（id={running.id}，{running.started_at}），拒绝重入")
    run = PipelineRun(status="running")
    session.add(run)
    session.flush()
    try:
        summary = compute_and_store_daily(session, Settings(), fetch_df=fetch_df)
        run.status, run.rows_fetched = "ok", 1
        run.error = str(summary)
        session.commit()
    except Exception as e:                          # noqa: BLE001 —— 先记账再重抛
        session.rollback()                          # 回滚整笔事务（含 running 行）
        from datetime import datetime
        err = PipelineRun(status="failed", finished_at=datetime.now(),
                          error=str(e)[:2000])      # 新事务补记失败流水
        session.add(err)
        session.commit()
        _safe_log("X", "pipeline", {"reason": "failed", "detail": str(e)[:150]})
        raise
    return run


# ---------------------------------------------------------------- §17.4 排查六步（一键体检）
def run_diagnosis(session: Session) -> dict:
    """排查 Runbook 六步自动执行（§17.4）。只读不写；每步 ok + 人话 detail。
    步5/步6 对齐"数学闭合结案"：快照差分 + 伸缩恒等式（±1 tick = 1e-6 元）。"""
    from vol import opslog
    steps: list[dict] = []

    # ① 数据到了吗：最近管线状态 + D|panel
    run = session.execute(select(PipelineRun).order_by(PipelineRun.id.desc())
                          .limit(1)).scalar_one_or_none()
    n_d = len(opslog.read_events(code="D", key="panel"))
    steps.append({"step": 1, "q": "数据到了吗", "ok": run is not None and run.status == "ok",
                  "detail": f"最近管线: {run.status if run else '无记录'}；D|panel 日志 {n_d} 条"})

    # ② 特征齐吗：最新 daily_metric 特征列有无 None
    dm = session.execute(select(DailyMetric).order_by(DailyMetric.date.desc())
                         .limit(1)).scalar_one_or_none()
    if dm is None:
        steps.append({"step": 2, "q": "特征齐吗", "ok": False, "detail": "daily_metric 为空"})
    else:
        holes = [c for c in ("iv_pct_120", "skew_25d", "momentum_20d") if getattr(dm, c) is None]
        steps.append({"step": 2, "q": "特征齐吗", "ok": not holes,
                      "detail": f"{dm.date}: " + ("特征完整" if not holes else f"缺 {holes}")})

    # ③ 弃权原因是什么：X 事件 + 最新预测 reason
    fc = session.execute(select(OpenForecast).order_by(OpenForecast.date.desc())
                         .limit(1)).scalar_one_or_none()
    n_x = len(opslog.read_events(code="X"))
    abst = bool(fc.abstain) if fc else None
    steps.append({"step": 3, "q": "弃权原因是什么",
                  "ok": abst is False,
                  "detail": ("尚无预测记录" if fc is None else
                             (f"{fc.date} 弃权: {fc.reason}" if abst else f"{fc.date} 正常出预测"))
                             + f"；X| 事件 {n_x} 条"})

    # ④ 预算为什么降级：最新 RiskBudget
    rb = session.execute(select(RiskBudget).order_by(RiskBudget.date.desc())
                         .limit(1)).scalar_one_or_none()
    reason4 = (rb.notes_json or {}).get("reason") if rb else None
    steps.append({"step": 4, "q": "预算为什么降级",
                  "ok": rb is not None and not rb.degraded,
                  "detail": ("尚无预算记录" if rb is None else
                             f"{rb.date}: confidence={rb.confidence}, degraded={rb.degraded}"
                             + (f", 原因: {reason4}" if reason4 else ""))})

    # ⑤⑤ 快照对账（阶跃点）+ ⑥ 数学闭合
    positions = session.execute(
        select(Position).where(func.upper(Position.status) == "OPEN")).scalars().all()
    jumps, closes = [], []
    for p in positions:
        snaps = session.execute(
            select(PositionSnapshot).where(PositionSnapshot.position_id == p.id)
            .order_by(PositionSnapshot.date)).scalars().all()
        if len(snaps) < 2:
            continue
        mvs = [s.mv for s in snaps]
        daily, cum = pr.pnl_daily_attribution(mvs)
        big = max(range(1, len(daily)), key=lambda i: abs(daily[i]))
        jumps.append(f"{p.code}: {snaps[big].date} 单日 {daily[big]:+.2f} 元")
        closes.append(abs(cum[-1] - (mvs[-1] - mvs[0])) <= 1e-6)
    n_pos = len(positions)
    steps.append({"step": 5, "q": "快照对账（阶跃点）", "ok": True,
                  "detail": (f"open 持仓 {n_pos} 个；最大阶跃: {'; '.join(jumps) if jumps else '无快照序列'}")})
    ok6 = all(closes) if closes else True
    steps.append({"step": 6, "q": "数学闭合结案", "ok": ok6,
                  "detail": ("全部持仓伸缩恒等式闭合（≤±1 tick）" if closes else "无快照序列，跳过")})

    rep = {"all_ok": all(s["ok"] for s in steps if s["step"] != 5), "steps": steps}
    _safe_log("P", "diagnosis", {"all_ok": rep["all_ok"], "steps": len(steps)})
    return rep


# ---------------------------------------------------------------- §12 模拟盘（M6-①）
def create_sim_account(session: Session, name: str, cash0: float,
                       auto_mirror: bool = False):
    """创建虚拟账户（与真实账户物理隔离，铁律 10 不受影响）。"""
    sa_ = SimAccountDB(name=name, cash0=cash0, auto_mirror=auto_mirror)
    session.add(sa_)
    session.commit()
    _safe_log("P", "sim_account", {"id": sa_.id, "cash0": cash0, "auto_mirror": auto_mirror})
    return sa_


def create_sim_order(session: Session, account_id: int, submit_date: date, expiry: date,
                     tpl_id: str | None = None, params: dict | None = None,
                     legs: list[dict] | None = None, qty: int = 1,
                     price_type: str = "market", limit_px: float | None = None):
    """下单：模板展开（legs 需要下单日 spot）或用户直填 legs。信息墙校验：
    submit_date 必须有行情截面（不存在 → 404 语义 LookupError）。"""
    acct = session.get(SimAccountDB, account_id)
    if acct is None:
        raise LookupError(f"sim_account {account_id} 不存在")
    dm = session.execute(
        select(DailyMetric).where(DailyMetric.date == submit_date)).scalar_one_or_none()
    if dm is None:
        raise LookupError(f"submit_date {submit_date} 当日无行情截面（信息墙：不许偷看/虚构未来日期）")
    if tpl_id:
        from vol.sim.templates import get_template
        legs = [l.__dict__ | {"expiry": (str(l.expiry) if l.expiry else None)} for l in
                get_template(tpl_id).generate_legs(dm.spot, expiry, qty, params)]  # 日历价差腿级到期
    if not legs:
        raise ValueError("legs 为空（模板展开失败或未提供）")
    o = SimOrderDB(account_id=account_id, tpl_id=tpl_id, params_json=params,
                   legs_json=legs, submit_date=submit_date, expiry=expiry,
                   price_type=price_type, limit_px=limit_px)
    session.add(o)
    session.commit()
    _safe_log("P", "sim_order", {"id": o.id, "account": account_id, "tpl": tpl_id,
                                 "submit": str(submit_date).replace("-", "")})
    return o


def _replay_setup(session: Session, from_date: date, to_date: date,
                  account_ids: list[int] | None = None):
    """批量/流式回放的公共准备：读截面（fail-closed ≥2 日）+ pending 单 + 内存账户。"""
    import pandas as pd
    rows = session.execute(
        select(DailyMetric.date, DailyMetric.spot, DailyMetric.atm_iv)
        .where(DailyMetric.date >= from_date, DailyMetric.date <= to_date)
        .order_by(DailyMetric.date)).all()
    if len(rows) < 2:
        raise RuntimeError("回放区间内面板数据不足 2 日（fail-closed）")
    df = pd.DataFrame(rows, columns=["date", "spot", "iv_near"])

    orders_q = (select(SimOrderDB)
                .where(SimOrderDB.submit_date >= from_date,
                       SimOrderDB.submit_date <= to_date,
                       SimOrderDB.status == "pending")
                .order_by(SimOrderDB.submit_date))
    if account_ids:
        orders_q = orders_q.where(SimOrderDB.account_id.in_(account_ids))
    order_rows = session.execute(orders_q).scalars().all()

    from vol.sim import SimAccountService, SimLeg, SimOrder
    svc = SimAccountService()
    accts = session.execute(select(SimAccountDB)).scalars().all()
    for a in accts:
        if account_ids is None or a.id in account_ids:
            svc.create(a.name, a.cash0, a.auto_mirror)
    sim_orders = []
    for o in order_rows:
        legs = [SimLeg(l["cp"], float(l["strike"]), l["side"], int(l["qty"]),
                       expiry=date.fromisoformat(l["expiry"]) if l.get("expiry") else None)
                for l in o.legs_json]                 # 日历价差：腿级到期日
        sim_orders.append(SimOrder(id=o.id, account_id=o.account_id, legs=legs,
                                   submit_date=o.submit_date, expiry=o.expiry,
                                   price_type=o.price_type, limit_px=o.limit_px,
                                   tpl_id=o.tpl_id))
    return df, order_rows, sim_orders, svc


def _replay_persist(session: Session, res, order_rows, sim_orders,
                    from_date: date, to_date: date) -> dict:
    """回放结果持久化：fills + 订单状态 + nav 覆盖 + 会话记录。幂等（nav 按 (account, ts) 覆盖）。"""
    leg_of = {o.id: o.legs for o in sim_orders}
    for f in res.fills:
        legs = leg_of.get(f["order_id"], [])
        idx = next((i for i, l in enumerate(legs)
                    if l.cp == f["cp"] and round(l.strike, 4) == round(f["strike"], 4)), 0)
        session.add(SimFillDB(order_id=f["order_id"], leg_index=idx, cp=f["cp"],
                              strike=f["strike"], side=f["side"], fill_px=f["fill_px"],
                              qty=f["qty"], cost=f["cost"],
                              filled_date=date.fromisoformat(f["date"])))
    for o in order_rows:
        matched = next((so for so in sim_orders if so.id == o.id), None)
        o.status = matched.status if matched else "cancelled"
    for aid, curve in res.nav.items():
        for ts, nav in curve:
            row = session.execute(select(SimNavDB).where(SimNavDB.account_id == aid,
                                                         SimNavDB.ts == ts)).scalar_one_or_none()
            if row is None:
                session.add(SimNavDB(account_id=aid, ts=ts, nav=nav))
            else:
                row.nav = nav
    rec = SimSessionRecDB(mode="replay", from_date=from_date, to_date=to_date,
                          summary_json={"days": res.days, "events": res.events,
                                        "fills": len(res.fills)})
    session.add(rec)
    session.commit()
    _safe_log("P", "sim_replay", {"days": res.days, "fills": len(res.fills),
                                  "events": len(res.events)})
    return {"session_id": rec.id, "days": res.days, "nav": {str(k): v for k, v in res.nav.items()},
            "benchmark": [[str(d), v] for d, v in res.benchmark],
            "fills": res.fills, "events": res.events}


def run_sim_replay(session: Session, from_date: date, to_date: date,
                   account_ids: list[int] | None = None) -> dict:
    """执行回放（同步，日频）：DailyMetric 截面 → 撮合 pending 单 → 净值打点 → 持久化。
    幂等：订单状态落库（filled/cancelled）后不重复成交；sim_nav 按 (account, ts) 覆盖。"""
    df, order_rows, sim_orders, svc = _replay_setup(session, from_date, to_date, account_ids)
    from vol.sim import ReplaySession
    res = ReplaySession(svc).run(df, sim_orders, from_date, to_date)
    return _replay_persist(session, res, order_rows, sim_orders, from_date, to_date)


def stream_sim_replay(session: Session, from_date: date, to_date: date,
                      account_ids: list[int] | None = None):
    """流式回放生成器（§12.4 GET /api/sim/stream 的 SSE 内核）：
    逐虚拟日 yield {"event": "day", "data": 快照}；全部日推进完才持久化并 yield "done"。
    客户端中途断开 → 生成器被关闭 → 不持久化（不完整会话不留痕，防脏状态）。"""
    df, order_rows, sim_orders, svc = _replay_setup(session, from_date, to_date, account_ids)
    from vol.sim import ReplaySession
    gen = ReplaySession(svc).run_iter(df, sim_orders, from_date, to_date)
    res = None
    while True:
        try:
            snap = next(gen)
        except StopIteration as e:
            res = e.value
            break
        yield {"event": "day", "data": snap}
    out = _replay_persist(session, res, order_rows, sim_orders, from_date, to_date)
    yield {"event": "done", "data": out}


# ---------------------------------------------------------------- 错单本（§12.4 游戏化复盘）
MIS_TRADE_LOSS = -500.0     # 亏损阈值（元）：订单浮动盈亏低于此收录错单本


def _intrinsic(spot: float, K: float, cp: str) -> float:
    return max(spot - K, 0.0) if cp == "C" else max(K - spot, 0.0)


def _order_pnl_rows(session: Session, from_date: date, to_date: date,
                    account_ids: list[int] | None = None) -> tuple[list[dict], date, float, float]:
    """区间内全部已成交订单的盈亏明细（错单本/归因瀑布共用，唯一实现）。
    订单级 pnl = 各腿当前市值(空头为负债) + 开仓现金流（含费用）——
    恒等式：期末净值 = 起始资金 + Σ订单pnl（区间内成交订单口径，瀑布图分毫不差的依据）。
    返回 (rows, last_date, spot_now, iv_now)。"""
    from vol.sim.broker import option_px
    q = (select(SimOrderDB).where(SimOrderDB.status == "filled",
                                  SimOrderDB.submit_date >= from_date,
                                  SimOrderDB.submit_date <= to_date)
         .order_by(SimOrderDB.submit_date))
    if account_ids:
        q = q.where(SimOrderDB.account_id.in_(account_ids))
    orders = session.execute(q).scalars().all()

    last_date, spot_now, iv_now = session.execute(
        select(DailyMetric.date, DailyMetric.spot, DailyMetric.atm_iv)
        .where(DailyMetric.date >= from_date, DailyMetric.date <= to_date)
        .order_by(DailyMetric.date.desc()).limit(1)).one()

    rows: list[dict] = []
    for o in orders:
        legs = o.legs_json
        fills = session.execute(select(SimFillDB)
                                .where(SimFillDB.order_id == o.id)
                                .order_by(SimFillDB.leg_index)).scalars().all()
        if not fills:
            continue
        flow = 0.0                                    # 开仓现金流（买付卖收，含费用）
        for f in fills:
            leg = legs[min(f.leg_index, len(legs) - 1)]
            signed = f.qty if leg["side"] == "buy" else -f.qty
            flow += -signed * f.fill_px * pr.UNIT - f.cost
        value = 0.0                                   # 各腿当前市值（空头为负）
        for leg in legs:
            signed = leg["qty"] if leg["side"] == "buy" else -leg["qty"]
            K = float(leg["strike"])
            leg_exp = date.fromisoformat(leg["expiry"]) if leg.get("expiry") else o.expiry
            if last_date >= leg_exp:                  # 已到期：内在价值结算口径
                px = _intrinsic(spot_now, K, leg["cp"])
            else:
                T = max((leg_exp - last_date).days, 1) / 365.0
                px = option_px(spot_now, K, T, iv_now, leg["cp"])
            value += signed * px * pr.UNIT
        rows.append({
            "order_id": o.id, "account_id": o.account_id,
            "submit_date": str(o.submit_date), "expiry": str(o.expiry),
            "tpl_id": o.tpl_id, "legs": legs,
            "fills": [{"leg_index": f.leg_index, "fill_px": f.fill_px, "qty": f.qty,
                       "cost": f.cost, "filled_date": str(f.filled_date)} for f in fills],
            "pnl": round(value + flow, 2),
        })
    return rows, last_date, spot_now, iv_now


def mis_trade_journal(session: Session, from_date: date, to_date: date,
                      account_ids: list[int] | None = None,
                      threshold: float = MIS_TRADE_LOSS) -> list[dict]:
    """错单本（借鉴 qsquiz 错题本）：区间内浮动盈亏 ≤ threshold 的订单自动收录，
    三段复盘：①当时信号怎么说 ②你做了什么 ③事后 IV 怎么走。
    现算现审不落库——数据源唯一（sim_order/sim_fill/daily_metric/signal），永不过期失真。"""
    rows, last_date, spot_now, iv_now = _order_pnl_rows(session, from_date, to_date, account_ids)

    out: list[dict] = []
    for r in rows:
        if r["pnl"] > threshold:
            continue
        o_id, o_submit, o_expiry = r["order_id"], date.fromisoformat(r["submit_date"]), \
            date.fromisoformat(r["expiry"])

        # ① 当时信号怎么说（提交日信号 + 建议卡结论）
        sig = session.execute(select(Signal).where(Signal.date == o_submit)
                              .order_by(Signal.id.desc()).limit(1)).scalar_one_or_none()
        if sig is not None:
            card = session.execute(select(AdviceCard).where(AdviceCard.signal_id == sig.id)
                                   .order_by(AdviceCard.id.desc()).limit(1)).scalar_one_or_none()
            signal_txt = f"{sig.signal}（{sig.engine} 引擎，置信度 {sig.confidence}）"
            if card is not None and card.card_json:
                signal_txt += f" → 建议卡 {card.verdict_json.get('verdict') if card.verdict_json else '—'}：{card.card_json.get('action', '')[:60]}"
        else:
            signal_txt = "该日无信号记录（管线未跑或自由发挥）"

        # ③ 事后 IV 怎么走
        entry = session.execute(select(DailyMetric.spot, DailyMetric.atm_iv)
                                .where(DailyMetric.date == o_submit)).one_or_none()
        iv_then = entry[1] if entry else None
        spot_then = entry[0] if entry else None
        iv_txt = (f"IV {iv_then*100:.2f}% → {iv_now*100:.2f}%"
                  f"（{(iv_now - iv_then)*100:+.2f} 个点）" if iv_then is not None
                  else f"期末 IV {iv_now*100:.2f}%（期初缺失）")
        iv_txt += f"；标的 {spot_then} → {spot_now}" if spot_then is not None else ""
        if o_expiry <= last_date:
            iv_txt += f"；持仓已到期（{o_expiry}），按内在价值结算"

        out.append({
            "order_id": o_id, "account_id": r["account_id"],
            "submit_date": r["submit_date"], "expiry": r["expiry"],
            "pnl": r["pnl"], "tpl_id": r["tpl_id"], "legs": r["legs"], "fills": r["fills"],
            "signal": signal_txt,
            "iv_story": iv_txt,
            "threshold": threshold,
        })
    _safe_log("S", "mis_trade", {"count": len(out), "range": f"{from_date}~{to_date}"})
    return out


def sim_compare(session: Session, from_date: date, to_date: date,
                account_ids: list[int] | None = None) -> dict:
    """多账户对比报告（§12.2 CompareReport：跟建议 vs 自由 vs 买入持有）。
    指标：期末净值 / 收益率 / 最大回撤（nav 序列口径）；基准=买入持有标的（1 万元归一）。"""
    q = select(SimAccountDB)
    if account_ids:
        q = q.where(SimAccountDB.id.in_(account_ids))
    accts = session.execute(q.order_by(SimAccountDB.id)).scalars().all()

    spots = session.execute(select(DailyMetric.date, DailyMetric.spot)
                            .where(DailyMetric.date >= from_date, DailyMetric.date <= to_date)
                            .order_by(DailyMetric.date)).all()
    if not spots:
        raise RuntimeError("区间内无行情截面（fail-closed）")
    spot0 = spots[0][1]
    bench_curve = [[str(d), round(10000.0 * s / spot0, 2)] for d, s in spots]

    rows = []
    for a in accts:
        navs = session.execute(
            select(SimNavDB.ts, SimNavDB.nav)
            .where(SimNavDB.account_id == a.id,
                   SimNavDB.ts >= from_date, SimNavDB.ts <= to_date)
            .order_by(SimNavDB.ts)).all()
        if not navs:
            continue
        curve = [(ts, nav) for ts, nav in navs]
        peak, mdd = curve[0][1], 0.0
        for _, nav in curve:
            peak = max(peak, nav)
            mdd = min(mdd, nav - peak)
        ret = curve[-1][1] - a.cash0
        rows.append({
            "account_id": a.id, "name": a.name, "auto_mirror": a.auto_mirror,
            "cash0": a.cash0, "final_nav": curve[-1][1],
            "return": round(ret, 2), "return_pct": round(ret / a.cash0 * 100, 2),
            "max_drawdown": round(mdd, 2), "days": len(curve),
            "curve": [[str(ts), nav] for ts, nav in curve],
        })
    _safe_log("S", "sim_compare", {"accounts": len(rows), "range": f"{from_date}~{to_date}"})
    return {"benchmark": {"name": "买入持有标的(1万)", "curve": bench_curve,
                          "return_pct": round((spots[-1][1] / spot0 - 1) * 100, 2)},
            "accounts": rows}


def sim_waterfall(session: Session, account_id: int, from_date: date, to_date: date) -> dict:
    """单账户归因瀑布（§12.4 复盘报告抽屉）：起始资金 → 逐订单盈亏 → 期末净值。
    恒等式口径（_order_pnl_rows）：Σ订单pnl = 期末净值 - 起始资金，瀑布分毫不差。"""
    a = session.get(SimAccountDB, account_id)
    if a is None:
        raise LookupError(f"sim_account {account_id} 不存在")
    rows, last_date, _, _ = _order_pnl_rows(session, from_date, to_date, [account_id])
    nav_last = session.execute(select(SimNavDB.nav)
                               .where(SimNavDB.account_id == account_id,
                                      SimNavDB.ts >= from_date, SimNavDB.ts <= to_date)
                               .order_by(SimNavDB.ts.desc()).limit(1)).scalar_one_or_none()
    steps = [{"label": "起始资金", "delta": a.cash0, "kind": "total"}]
    for r in rows:
        steps.append({"label": f"#{r['order_id']} {r['submit_date'][5:]} "
                               f"{(r['tpl_id'] or '自由')}",
                      "delta": r["pnl"], "kind": "order", "order_id": r["order_id"]})
    end = nav_last if nav_last is not None else a.cash0 + sum(r["pnl"] for r in rows)
    steps.append({"label": "期末净值", "delta": round(end, 2), "kind": "total"})
    return {"account_id": account_id, "name": a.name, "asof": str(last_date),
            "steps": steps,
            "identity_ok": abs(sum(r["pnl"] for r in rows) + a.cash0 - end) < 0.05}


# ---------------------------------------------------------------- M6-③ 命中率周度复核
REVIEW_MIN_SAMPLES = 30      # 有效样本下限，不足则跳过本次记录
REVIEW_WARN_STREAK = 4       # 连续 N 周跌破基准 → warn（面板黄色警告）


def run_weekly_review(session: Session, asof: date | None = None) -> dict:
    """命中率周度复核（M6-③）：m2 缓存面板 → analog_states walk-forward →
    滚动样本外方向命中率 vs 朴素基准（多数方向命中率）→ ReviewLog（同周覆盖）。
    by_regime_json 按 高/低 IV 期（IV 中位数）分组命中率。
    warn = 最近 REVIEW_WARN_STREW 周（含本周）全部 hit_rate < baseline。
    读缓存为主，缺新日自动补拉；样本不足 fail-soft 跳过不落库。"""
    import numpy as np
    from m2_run import WINDOW, HORIZON, build_daily_history
    from m3_run import analog_states
    df = build_daily_history()
    ana = analog_states(df)
    hits, actuals, hi_hits, lo_hits = [], [], [], []
    med = float(df["iv_near"].median())
    for i in range(WINDOW, len(df["iv_near"]) - HORIZON):
        if ana[i] == 0:
            continue
        actual = df["iv_near"][i + HORIZON] - df["iv_near"][i]
        if abs(actual) <= 0.001:
            continue
        h = (ana[i] > 0) == (actual > 0)
        hits.append(h)
        actuals.append(actual > 0)
        (hi_hits if df["iv_near"][i] > med else lo_hits).append(h)
    n = len(hits)
    if n < REVIEW_MIN_SAMPLES:
        _safe_log("X", "weekly_review", {"skip": "low_samples", "n": n})
        return {"recorded": False, "samples": n, "reason": f"有效样本 {n} < {REVIEW_MIN_SAMPLES}"}
    hit_rate = float(np.mean(hits))
    baseline = float(max(np.mean(actuals), 1 - np.mean(actuals)))

    ref = asof or date.today()
    iso = ref.isocalendar()
    week = f"{iso.year}-W{iso.week:02d}"
    row = session.execute(select(ReviewLog).where(ReviewLog.week == week)).scalar_one_or_none()
    if row is None:
        row = ReviewLog(week=week)
        session.add(row)
    row.sample_n, row.hit_rate, row.baseline = n, round(hit_rate, 4), round(baseline, 4)
    row.by_regime_json = {
        "panel_days": len(df),
        "hit_rate_high_iv": round(float(np.mean(hi_hits)), 4) if hi_hits else None,
        "hit_rate_low_iv": round(float(np.mean(lo_hits)), 4) if lo_hits else None,
        "edge": round(hit_rate - baseline, 4),
    }
    session.flush()

    # 连续 N 周跌破基准（含本周）→ warn；本周未跌破则清零
    recent = session.execute(select(ReviewLog).order_by(ReviewLog.id.desc())
                             .limit(REVIEW_WARN_STREAK)).scalars().all()
    row.warn = (len(recent) >= REVIEW_WARN_STREAK
                and all(r.hit_rate < r.baseline for r in recent))
    session.commit()
    _safe_log("S", "weekly_review", {"week": week, "n": n,
                                     "hit": round(hit_rate, 4), "base": round(baseline, 4),
                                     "warn": row.warn})
    return {"recorded": True, "week": week, "samples": n,
            "hit_rate": round(hit_rate, 4), "baseline": round(baseline, 4),
            "edge": round(hit_rate - baseline, 4), "warn": row.warn}
