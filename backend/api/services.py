# -*- coding: utf-8 -*-
"""业务服务层：①建议卡片生成（风控链 trace）②每日信号管线（复用 m2/m3 已迁移的领域包）。
铁律 7：任何一步失败即 fail-closed（不出卡 / 管线标记 failed）。铁律 8：T+1 过期作废。"""
import sys
from datetime import date
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from db.models import (AdviceCard, DailyMetric, OpenForecast, PipelineRun,
                       Position, PositionSnapshot, RiskBudget, Signal)
from vol.calendar import get_calendar
from vol.config import Settings

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:          # m2/m3 在仓库根
    sys.path.insert(0, str(_ROOT))

_ACTION = {"SHORT_VOL": "倾向卖波（收权利金方向）", "LONG_VOL": "倾向买波（波动率多头方向）",
           "NEUTRAL": "观望", "WAIT": "观望", "INSUFFICIENT_HISTORY": "观望（历史不足）"}


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
        select(Position.id).where(Position.status == "open")).scalars().all())
    vega = sum(r.vega for r in rows if r.position_id in open_ids)
    delta = sum(r.delta for r in rows if r.position_id in open_ids)
    return latest, vega, delta


def generate_advice(session: Session, settings: Settings, signal_id: int) -> AdviceCard:
    """由信号生成建议卡：跑风控条件链，全程留 trace（verdict_json）。只建议，不下单。"""
    sig = session.get(Signal, signal_id)
    if sig is None:
        raise LookupError(f"signal {signal_id} 不存在")

    checks: list[dict] = []
    today = date.today()

    # ① 有效期（铁律 8：T+1 收盘信号次日有效，过期作废）
    valid = sig.valid_until is not None and sig.valid_until >= today
    checks.append({"name": "signal_valid_until", "passed": valid,
                   "detail": f"valid_until={sig.valid_until}, today={today}"})

    # ② Vega 限额
    snap_date, vega, delta = latest_exposure(session)
    vega_ok = abs(vega) <= settings.vega_limit
    checks.append({"name": "vega_limit", "passed": vega_ok,
                   "detail": f"组合vega={vega:.1f} (每1.0 IV口径), 限额±{settings.vega_limit}, 快照日={snap_date}"})

    # ③ Delta 限额
    delta_ok = abs(delta) <= settings.delta_limit
    checks.append({"name": "delta_limit", "passed": delta_ok,
                   "detail": f"组合delta={delta:.1f}, 限额±{settings.delta_limit}"})

    hard_fail = not valid
    soft_warn = not (vega_ok and delta_ok)
    verdict = "REJECT" if hard_fail else ("WARN" if soft_warn else "PASS")

    action = _ACTION.get(sig.signal, "观望")
    if isinstance(sig.detail_json, dict) and sig.detail_json.get("confirm_needed", 0) > 0:
        action += f"（状态翻转待确认，还需 {sig.detail_json['confirm_needed']} 日）"
    if verdict == "REJECT":
        action = "已过期，执行前必须基于最新信号重新生成建议"
    if verdict == "WARN":
        action += "；⚠️ 组合敞口接近/超出限额，先减仓再考虑新头寸"

    card = {
        "date": str(sig.date), "signal": sig.signal, "action": action,
        "engine": sig.engine, "confidence": sig.confidence,
        "valid_until": str(sig.valid_until) if sig.valid_until else None,
        "exec_window": _exec_window(sig.date),
        "disclaimer": "本系统只生成建议，不自动下单；执行前必须用 T+1 最新行情复核风控条件链",
    }
    ac = AdviceCard(signal_id=sig.id, verdict_json={"verdict": verdict, "checks": checks},
                    card_json=card)
    session.add(ac)
    session.flush()
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
        raise RuntimeError("面板数据不可用（fail-closed）")

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
    return {"date": d_str, "percentile": pct["signal"], "analog": ana_sig,
            "agree": agree, "iv_now": round(iv_now, 4), "iv_pct_120": pct.get("iv_pct")}


LAMBDA_MAP = {"conservative": 8.0, "balanced": 4.0, "aggressive": 2.0}   # §14.1 三档


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
        return row
    except Exception as e:                          # noqa: BLE001 —— 预测失败不拖垮管线
        row = session.get(OpenForecast, d)
        if row is None:
            row = OpenForecast(date=d)
            session.add(row)
        row.abstain, row.reason = True, f"预测管线异常（fail-closed）: {type(e).__name__}: {e}"[:500]
        session.flush()
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
        return row


def _open_theta_annual(session: Session) -> float:
    """最新快照日 open 持仓的年化 Theta（正式库 theta 列已是年化口径，铁律 2）。"""
    latest = session.execute(select(func.max(PositionSnapshot.date))).scalar_one()
    if latest is None:
        return 0.0
    open_ids = set(session.execute(
        select(Position.id).where(Position.status == "open")).scalars().all())
    rows = session.execute(select(PositionSnapshot).where(PositionSnapshot.date == latest)).scalars().all()
    return float(sum(r.theta for r in rows if r.position_id in open_ids))


def _fetch_open_map(days: int = 30) -> dict | None:
    """T+1 开盘价映射（§13 标签/晨检复核）。联网失败返回 None → 预测自然弃权（fail-closed）。"""
    try:
        import akshare as ak
        hist = ak.fund_etf_hist_em(symbol="510050", period="daily",
                                   start_date="20240101", end_date="20991231", adjust="")
        hist["日期"] = hist["日期"].astype(str).str[:10]
        tail = hist.tail(days)
        return dict(zip(tail["日期"], tail["开盘"].astype(float)))
    except Exception:
        return None


def trigger_pipeline(session: Session, fetch_df=None) -> PipelineRun:
    """防重入锁：存在 running 记录即拒绝（铁律 9：单一入口）。失败 fail-closed 落 failed 记录。"""
    running = session.execute(select(PipelineRun).where(PipelineRun.status == "running")
                              .limit(1)).scalar_one_or_none()
    if running is not None:
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
        raise
    return run
