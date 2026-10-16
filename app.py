# -*- coding: utf-8 -*-
"""
app.py — 波动率决策辅助系统 · Streamlit 面板（M4）
五 Tab：IV监控 / 信号台 / 风控台 / 归因 / 回测
数据读本地缓存（data/cache）+ 正式库 data/vol.db（M5-⑤ 切换，demo 库 vol_demo.db 仅作回退），"刷新数据"按钮触发显式联网。
启动：streamlit run app.py --server.port 8501
"""
import math
import os
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import sys, os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "backend"))


from vol.pricing.curve import fit_curve_iv
from vol.calendar import get_calendar
from run_demo import R_FREE, UNDERLYING, UNIT
from m2_run import CACHE_DIR, WINDOW, HORIZON, load_day, build_daily_history, fetch_dates, expiry_from_code
from m3_run import percentile_states, analog_states, backtest, replay_position, FORMAL_DB_PATH as DB_PATH
from vol.signals import PercentileEngine

st.set_page_config(page_title="波动率决策辅助系统", page_icon="📊", layout="wide")

SWITCH_COST = 30.0
VEGA_NOTIONAL = 100.0


@st.cache_data(ttl=300, show_spinner=False)
def _position_review(db_path):
    """持仓风险评审（§20.2 P1）：保证金上界 + 浮亏分级 + 到期周。
    算术全部委托 vol.position_risk 纯函数（与 services.position_risk_review 同源）。"""
    import sqlite3 as _sq
    from datetime import date as _d2
    from vol import position_risk as pr
    cal = get_calendar()
    con = _sq.connect(db_path)
    try:
        rows = []
        for pid, code, qty, open_px in con.execute(
                "SELECT id, code, qty, open_px FROM position WHERE upper(status)='OPEN'").fetchall():
            r2 = con.execute("SELECT date, mv, spot FROM position_snapshot WHERE position_id=? "
                             "ORDER BY date DESC LIMIT 2", (pid,)).fetchall()
            if not r2:
                continue
            d, mv, spot = r2[0]
            ratio, level = pr.loss_level(mv, qty, open_px)
            cur_px = abs(mv) / (abs(qty) * pr.UNIT) if qty else 0.0
            margin = pr.estimate_margin_short(cur_px, spot) * abs(qty) if qty < 0 else 0.0
            exp = pr.parse_expiry(code)
            td = cal.countdown(_d2.fromisoformat(d), exp) if exp else None
            # §20.2 #5：前一日快照作前结算代理，校验今日价是否越涨跌停区间
            hit = None
            if len(r2) == 2 and abs(qty) > 0:
                prev_px = abs(r2[1][1]) / (abs(qty) * pr.UNIT)
                band = pr.price_limit_band(prev_px, r2[1][2], pr.kind_from_code(code))
                if band and not (band[0] <= cur_px <= band[1]):
                    hit = "UP" if cur_px > band[1] else "DOWN"
            rows.append({"code": code, "ratio": ratio, "level": level, "margin": margin,
                         "td": td, "exp": str(exp) if exp else None, "limit_hit": hit})
        return rows
    finally:
        con.close()


def cache_version():
    fps = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)] if os.path.isdir(CACHE_DIR) else []
    return max((os.path.getmtime(f) for f in fps), default=0.0)


@st.cache_data(ttl=600, show_spinner="加载本地数据 ...")
def load_df(v):
    days = fetch_dates(250)
    rows = []
    for d, px in days:
        fp = os.path.join(CACHE_DIR, f"ri_v2_{d.replace('-', '')}.csv")
        if not os.path.exists(fp):
            continue
        t = pd.read_csv(fp, dtype={"code": str})
        t["exp_dt"] = t["code"].map(expiry_from_code)
        rows.append((d, px, t))
    recs = []
    for d, px, t in rows:
        d0 = pd.Timestamp(d)
        t = t[(t["exp_dt"] - d0).dt.days >= 14]
        t = t[t["iv"] > 0]
        if len(t) == 0:
            continue
        exps = sorted(t["exp_dt"].unique())
        sub0 = t[t["exp_dt"] == exps[0]]
        iv_near = float(sub0.iloc[(sub0["strike"] - px).abs().argsort().iloc[0]]["iv"])
        iv_next = None
        if len(exps) > 1:
            sub1 = t[t["exp_dt"] == exps[1]]
            iv_next = float(sub1.iloc[(sub1["strike"] - px).abs().argsort().iloc[0]]["iv"])
        calls, puts = sub0[sub0["cp"] == "C"], sub0[sub0["cp"] == "P"]
        try:
            sk = (float(puts.iloc[(puts["delta"] + 0.25).abs().argsort().iloc[0]]["iv"])
                  - float(calls.iloc[(calls["delta"] - 0.25).abs().argsort().iloc[0]]["iv"]))
        except Exception:
            sk = 0.0
        recs.append({"date": d, "spot": px, "iv_near": iv_near, "iv_next": iv_next, "skew25": sk})
    df = pd.DataFrame(recs)
    df["rvol20"] = np.log(df["spot"]).diff().rolling(20).std() * math.sqrt(245)
    return df


@st.cache_data(ttl=600, show_spinner=False)
def latest_curve(v):
    """最新"有缓存快照"一日的近月截面（聚合 C/P）→ SVI 拟合 + 偏离。
    fetch_dates 返回 ETF 日历（可能领先于指标缓存），必须取最后一个有 ri_v2_* 文件的日期，
    否则节假日/未刷新日直接 FileNotFoundError 白屏。"""
    days = fetch_dates(250)
    d = px = None
    for d_, px_ in reversed(days):
        if os.path.exists(os.path.join(CACHE_DIR, f"ri_v2_{d_.replace('-', '')}.csv")):
            d, px = d_, px_
            break
    if d is None:
        raise FileNotFoundError("缓存中没有任何期权快照（ri_v2_*）——先点侧栏'刷新数据'")
    t = pd.read_csv(os.path.join(CACHE_DIR, f"ri_v2_{d.replace('-', '')}.csv"), dtype={"code": str})
    t["exp_dt"] = t["code"].map(expiry_from_code)
    d0 = pd.Timestamp(d)
    t = t[(t["exp_dt"] - d0).dt.days >= 14]
    t = t[t["iv"] > 0]
    exp_day = t["exp_dt"].min()
    near = t[t["exp_dt"] == exp_day].copy()
    T = max((exp_day - d0).days, 1) / 365.0
    agg = near.groupby("strike", as_index=False)["iv"].mean().sort_values("strike")
    F = px * math.exp(R_FREE * T)
    return d, px, exp_day.strftime("%Y-%m-%d"), T, F, agg, near


def _curve_meta():
    """拟合（毫秒级）放缓存外执行：SVI 闭包不可 pickle，不能进 cache_data。"""
    d, px, exp_str, T, F, agg, near = latest_curve(cache_version())
    fn, r2, model = fit_curve_iv(agg["strike"].tolist(), agg["iv"].tolist(), px, T, R_FREE)
    near = near.copy()
    near["dev_bp"] = near.apply(
        lambda r: (r["iv"] - fn(math.log(r["strike"] / F))) * 10000 if r["iv"] > 0 else np.nan, axis=1)
    return d, px, exp_str, T, F, fn, r2, model, near


@st.cache_data(ttl=600, show_spinner="回测计算 ...")
def run_backtest(v, df):
    pct_states = percentile_states(df["iv_near"].tolist())
    ana_states = analog_states(df)
    return pct_states, ana_states, backtest(df, pct_states, ana_states)


def max_drawdown(nav):
    peak, mdd = nav[0], 0.0
    for x in nav:
        peak = max(peak, x)
        mdd = min(mdd, x - peak)
    return mdd


# ---------- 侧边栏 ----------

st.sidebar.title("波动率决策辅助系统")
st.sidebar.caption(f"标的 510050 · {datetime.now().strftime('%Y-%m-%d %H:%M')}")
v = cache_version()
df = load_df(v)
st.sidebar.metric("本地历史", f"{len(df)} 交易日")
st.sidebar.caption(f"{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
cal = get_calendar()
st.sidebar.caption(cal.status_label())


@st.cache_data(ttl=60)
def _iv_degrade_state() -> bool:
    """§17.6 降级角标：正式库最近 3 日 iv_pct_120 连续两日单日变动 > 0.30 → True。
    与 services._iv_jump_review 同阈值同口径（此处只读不写）。"""
    import sqlite3 as _sq
    con = _sq.connect(DB_PATH)
    try:
        rows = con.execute("SELECT iv_pct_120 FROM daily_metric ORDER BY date DESC LIMIT 3").fetchall()
    finally:
        con.close()
    vals = [r[0] for r in rows if r[0] is not None]
    if len(vals) < 3:
        return False
    return all(abs(vals[i] - vals[i + 1]) > 0.30 for i in range(2))


if _iv_degrade_state():
    st.sidebar.error("⚠️ IV 连续两日跳变（>30 分位）——信号链降级中，结论仅作参考")


st.sidebar.caption("铁律：只输出建议，永不自动下单；\n信号 T 日生成、T+1 执行窗口内有效。")

if st.sidebar.button("刷新数据（显式联网，约 30 秒）"):
    with st.spinner("拉取最新官方风险指标 ..."):
        build_daily_history()
    st.cache_data.clear()
    st.sidebar.success("已更新")

tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
    ["📈 IV 监控", "🎯 信号台", "🛡 风控台", "💰 持仓归因", "🔬 回测", "🎲 模拟盘"])

# ---------- Tab1 IV 监控 ----------

with tab1:
    d, px, exp_str, T, F, fn, r2, model, near = _curve_meta()
    _exp_dt = pd.Timestamp(exp_str).date()
    _d_dt = pd.Timestamp(d).date()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("510050 现价", f"{px:.3f}")
    c2.metric("近月到期", exp_str,
              f"T-{cal.countdown(_d_dt, _exp_dt)} 个交易日（下一交易日 {cal.next_trading_day(_d_dt)}）")
    c3.metric("ATM IV", f"{df['iv_near'].iloc[-1] * 100:.2f}%")
    c4.metric("曲线拟合", f"{model} R²={r2:.3f}")

    fig = go.Figure()
    agg = near.groupby("strike", as_index=False)["iv"].mean().sort_values("strike")
    fig.add_trace(go.Scatter(x=agg["strike"], y=agg["iv"] * 100, mode="markers",
                             name="市场IV", marker=dict(color="#185FA5", size=9)))
    grid = np.linspace(agg["strike"].min(), agg["strike"].max(), 60)
    fig.add_trace(go.Scatter(x=grid, y=[fn(math.log(k / F)) * 100 for k in grid], mode="lines",
                             name=f"拟合({model})", line=dict(color="#D85A30", width=2.5)))
    fig.update_layout(title=f"近月波动率曲线（{d}）", xaxis_title="行权价",
                      yaxis_title="IV %", template="plotly_white", height=380)
    st.plotly_chart(fig, use_container_width=True)

    hist = go.Figure()
    vs = df["iv_near"] * 100
    hist.add_trace(go.Scatter(x=df["date"], y=vs, mode="lines", name="近月ATM IV",
                              line=dict(color="#185FA5", width=1.8)))
    p20, p80 = np.percentile(vs, 20), np.percentile(vs, 80)
    for y, c, t in [(p80, "#A32D2D", "80分位"), (p20, "#3B6D11", "20分位")]:
        hist.add_hline(y=y, line_dash="dash", line_color=c, annotation_text=f"{t} {y:.1f}%")
    hist.update_layout(title=f"ATM IV 历史（{len(df)} 交易日）", template="plotly_white", height=340)
    st.plotly_chart(hist, use_container_width=True)

# ---------- Tab2 信号台 ----------

with tab2:
    eng = PercentileEngine(window=WINDOW)
    sig = eng.run(df["iv_near"].tolist())
    pct_states, ana_states, _ = run_backtest(v, df)

    # ---- §18.6 今日建议（滞留-采纳闭环）：T 收盘生成 → T+1 呈现 → 挂到采纳/忽略/过期 ----
    try:
        import sqlite3 as _sq
        from datetime import date as _date
        _con = _sq.connect(DB_PATH)
        _today = _date.today().isoformat()
        _stale = _con.execute(
            """UPDATE advice_card SET status='expired'
               WHERE status='pending' AND signal_id IN
                 (SELECT id FROM signal WHERE valid_until < ?)""", (_today,))
        _con.commit()
        _row = _con.execute(
            """SELECT a.id, s.date, s.engine, s.signal, s.valid_until, a.created_at
               FROM advice_card a JOIN signal s ON a.signal_id = s.id
               WHERE a.status='pending' AND s.valid_until >= ?
               ORDER BY a.id DESC LIMIT 1""", (_today,)).fetchone()
        if _stale.rowcount:
            st.toast(f"已清算 {_stale.rowcount} 张过期未采纳的建议卡（留痕不删）")
        _con.close()
        if _row:
            _aid, _sd, _eng_name, _sig_name, _vu, _ca = _row
            with st.container(border=True):
                cA, cB, cC = st.columns([3, 1, 1])
                with cA:
                    st.markdown(f"### 📌 今日建议（待采纳）—— {_sig_name} · {_eng_name} 引擎")
                    st.caption(f"信号日 {_sd} 收盘生成 · 有效期至 {_vu}（T+1，铁律 8）· "
                               f"生成于 {_ca} · 每日滞留呈现，直到采纳/忽略/过期")
                    # §20.2 P1 角标：保证金上界 / 浮亏分级 / 到期周（机器算好，扫一眼）
                    _rv = _position_review(DB_PATH)
                    if _rv:
                        _badges = []
                        _mm = sum(r["margin"] for r in _rv)
                        if _mm:
                            _badges.append(f"💰 保证金占用估算（上界）≈ {_mm:,.0f} 元")
                        _worst = max((r["level"] for r in _rv),
                                     key=lambda x: {"STRONG": 2, "WARN": 1}.get(x, 0), default=None)
                        if _worst == "STRONG":
                            _badges.append("🚨 浮亏 ≥100% 应收权利金")
                        elif _worst == "WARN":
                            _badges.append("⚠️ 浮亏 ≥50% 应收权利金")
                        _ew = [f"T-{r['td']}" for r in _rv if r["td"] is not None and r["td"] <= 5]
                        if _ew:
                            _badges.append(f"⏳ 到期周（剩 {'、'.join(_ew)} 个交易日）")
                        _hits = [r["code"] for r in _rv if r.get("limit_hit")]
                        if _hits:
                            _badges.append(f"🚧 触及涨跌停区间（{'、'.join(_hits)}）——今日谨慎挂价")
                        if _badges:
                            (_st.error if _worst == "STRONG" else _st.warning)(" · ".join(_badges))
                with cB:
                    if st.button("✅ 采纳", key=f"adopt_{_aid}", use_container_width=True):
                        _c2 = _sq.connect(DB_PATH)
                        _c2.execute("UPDATE advice_card SET status='adopted', adopted_at=CURRENT_TIMESTAMP "
                                    "WHERE id=? AND status='pending'", (_aid,))
                        _c2.commit(); _c2.close()
                        st.success("已采纳（留痕：几点采纳）")
                        st.rerun()
                with cC:
                    if st.button("✖️ 忽略", key=f"dismiss_{_aid}", use_container_width=True):
                        _c3 = _sq.connect(DB_PATH)
                        _c3.execute("UPDATE advice_card SET status='dismissed' "
                                    "WHERE id=? AND status='pending'", (_aid,))
                        _c3.commit(); _c3.close()
                        st.info("已忽略本次建议")
                        st.rerun()
        else:
            st.caption("今日无待采纳建议（已全部处理或尚无信号）")
        st.divider()
    except Exception:
        pass  # 建议区故障不阻断信号台（fail-loud-not-crash）

    col1, col2 = st.columns(2)
    with col1:
        st.subheader("引擎 A · IV 分位数")
        color = {"SHORT_VOL": "🟠", "LONG_VOL": "🟢", "NEUTRAL": "⚪"}.get(sig["signal"], "⚪")
        st.markdown(f"### {color} {sig.get('signal', '—')}")
        st.write(f"当前 IV **{sig.get('iv_now', 0) * 100:.2f}%** · "
                 f"{WINDOW} 日分位 **{sig.get('iv_pct', '—')}** · "
                 f"连续 {sig.get('streak', 0)} 日处于高/低区"
                 + (f"（还需 {sig['confirm_needed']} 日确认）" if sig.get("confirm_needed") else "（已确认）"))
        st.caption(f"⚠️ 窗口敏感性：40 日窗口分位 = "
                   f"{(df['iv_near'].tail(40) <= df['iv_near'].iloc[-1]).mean():.2f}，"
                   f"120 日窗口分位 = {sig.get('iv_pct', '—')} —— 窗口选择影响巨大，面板双窗口对照")
    with col2:
        st.subheader("引擎 B · 历史情景类比")
        ana_now = ana_states[-1] if ana_states else 0
        direction = {1: "🟢 偏向买波（IV 看涨）", -1: "🟠 偏向卖波（IV 看跌）", 0: "⚪ 无共识，观望"}[ana_now]
        st.markdown(f"### {direction}")
        hits = []
        for i in range(WINDOW, len(df) - HORIZON):
            if ana_states[i] != 0:
                actual = df["iv_near"][i + HORIZON] - df["iv_near"][i]
                pred = ana_states[i]
                hits.append((pred > 0) == (actual > 0))
        if hits:
            base = max((sum(1 for a in (df["iv_near"][i + HORIZON] - df["iv_near"][i]
                                         for i in range(WINDOW, len(df) - HORIZON)) if a > 0) / len(hits),
                        1 - sum(1 for a in (df["iv_near"][i + HORIZON] - df["iv_near"][i]
                                            for i in range(WINDOW, len(df) - HORIZON)) if a > 0) / len(hits)))
            st.write(f"滚动方向命中率 **{np.mean(hits):.1%}**（样本 {len(hits)}，"
                     f"朴素基准 ≈ {base:.1%}）")
        st.caption("样本仍短（250 交易日），命中率每周复核，显著跑赢朴素基准前只做参考")

    st.divider()
    st.subheader("候选合约（近月，按 |偏离| 排序，流动性闸门 ≥40）")
    tbl = near.copy()
    tbl["liq"] = np.nan  # 实时快照需联网，点击侧栏刷新后由批处理补齐
    good = tbl[tbl["dev_bp"].notna()].sort_values("dev_bp", key=lambda s: s.abs(), ascending=False)
    st.dataframe(good[["sym", "strike", "cp", "iv", "delta", "vega", "theta", "dev_bp"]]
                 .rename(columns={"sym": "合约", "strike": "行权价", "cp": "类型", "iv": "官方IV",
                                  "delta": "Delta", "vega": "Vega(1.0IV)", "theta": "Theta(年化)",
                                  "dev_bp": "偏离bp"}),
                 use_container_width=True, height=320)
    st.info("⏱ 执行时序：本页信号基于 **" + d + "** 收盘截面，**T+1 日 9:35~10:00** 执行窗口内有效；"
            "执行前用最新行情复核，过期自动作废。系统只生成建议，执行在您手中。")

# ---------- Tab3 风控台 ----------

with tab3:
    st.subheader("组合敞口 vs 上限（示例持仓：卖 1 张 ATM 跨式）")
    last = ledger_last = None
    try:
        con = pd.read_sql_query("SELECT * FROM position_snapshot ORDER BY date DESC LIMIT 1", f"sqlite:///{DB_PATH}")
        if len(con):
            r = con.iloc[0]
            c1, c2 = st.columns(2)
            vega_lim = st.sidebar.number_input("Vega 上限（元/1.0IV）", value=3000, key="vl")
            delta_lim = st.sidebar.number_input("Delta 上限（元）", value=15000, key="dl")
            c1.metric("组合 Vega 敞口", f"{r['vega']:+.0f}", f"上限 ±{vega_lim}")
            c1.progress(min(abs(r['vega']) / vega_lim, 1.0))
            c2.metric("组合 Delta 敞口", f"{r['delta']:+.0f}", f"上限 ±{delta_lim}")
            c2.progress(min(abs(r['delta']) / delta_lim, 1.0))
            st.caption(f"快照日 {r['date']} · fail-closed：参数未显式设置时一律拒绝开新仓")
        else:
            st.info("暂无持仓快照 —— 先在归因 Tab 生成示例持仓")
    except Exception:
        st.info("暂无账本数据 —— 运行归因 Tab 的示例回放后显示")

    # §17.4 排查六步一键体检（只读，直查正式库）
    with st.expander("🔧 运维体检（排查六步）"):
        if st.button("运行体检", key="btn_diag"):
            try:
                from api.services import run_diagnosis
                from db.session import make_engine, make_session
                SM, _ = make_session(make_engine(f"sqlite:///{DB_PATH}"))
                rep = run_diagnosis(SM())
                for s_ in rep["steps"]:
                    icon = "✅" if s_["ok"] or s_["step"] == 5 else ("ℹ️" if s_["step"] == 5 else "⚠️")
                    st.markdown(f"**{icon} 步{s_['step']} · {s_['q']}** — {s_['detail']}")
                st.caption("总体: " + ("全绿 ✅" if rep["all_ok"] else "存在 ⚠️ 项，按步排查"))
            except Exception as e:                     # noqa: BLE001 —— 体检失败不影响主面板
                st.warning(f"体检失败：{e}")
    st.divider()
    st.markdown("**风控条件链（建议仓位逐级收窄）**")
    chain = [
        ("Vega 上限", "abs(Σvega) ≤ 权利金余额 × 30%"),
        ("Delta 上限", "abs(Σdelta×S×10000) ≤ 账户净值 × 5%"),
        ("单一行权价仓位", "每 strike ≤ 总仓 30%"),
        ("流动性闸门", "流动性分 ≥ 40，低于禁止新开"),
        ("深虚/深实", "|delta| < 0.05 或 > 0.95 只准平仓"),
        ("资金占用", "权利金净支出 ≤ 可用资金 50%"),
        ("信号一致性", "新仓方向必须与信号状态机一致"),
    ]
    for name, rule in chain:
        st.markdown(f"- **{name}**：{rule}")

# ---------- Tab4 归因 ----------

with tab4:
    st.subheader("模拟持仓快照差分归因")
    if st.button("生成/刷新示例持仓回放（20 交易日，读本地缓存）"):
        replay_position({d: pd.read_csv(os.path.join(CACHE_DIR, f"ri_v2_{d.replace('-', '')}.csv"),
                                        dtype={"code": str})
                         .assign(exp_dt=lambda x: x["code"].map(expiry_from_code))
                         for d in df["date"]} | {}, df)
        st.cache_data.clear()
    try:
        snap = pd.read_sql_query("SELECT * FROM position_snapshot ORDER BY date", f"sqlite:///{DB_PATH}")
        pos = pd.read_sql_query("SELECT * FROM position LIMIT 1", f"sqlite:///{DB_PATH}")
        if len(snap):
            _qty = int(pos.iloc[0]['qty'])
            st.success(f"持仓：{'空头' if _qty < 0 else '多头'} × {abs(_qty)} 组 · {pos.iloc[0]['code']}（开仓 {pos.iloc[0]['open_date']}）")
            cum_pnl = snap['mv'].diff().fillna(0).cumsum()   # 快照差分口径（铁律：展示必差分）
            c1, c2, c3 = st.columns(3)
            c1.metric("累计盈亏（市值差分）", f"{cum_pnl.iloc[-1]:+.0f} 元")
            c2.metric("组合 Vega", f"{snap['vega'].iloc[-1]:+.0f}")
            c3.metric("Theta（年化口径）", f"{snap['theta'].iloc[-1]:+.0f}")
            # §20.2 P1：保证金占用 / 浮亏触发线 / 到期周
            _rv = _position_review(DB_PATH)
            if _rv:
                _r = _rv[0]
                _lvl = {"STRONG": "🚨 STRONG（≥100% 权利金）", "WARN": "⚠️ WARN（≥50% 权利金）"}.get(_r["level"], "正常")
                d1, d2, d3 = st.columns(3)
                d1.metric("保证金占用（估算上界）", f"{_r['margin']:,.0f} 元",
                          help="上交所最低标准公式：[px + Max(12%·px, 10%·spot)]×10000；不扣虚值 → 上界偏保守")
                d2.metric("浮亏 / 应收权利金", f"{_r['ratio']*100:.0f}%", _lvl)
                d3.metric("到期倒计时", f"T-{_r['td']} 个交易日" if _r["td"] is not None else "未知",
                          "⏳ 到期周：注意 pin risk/指派" if (_r["td"] is not None and _r["td"] <= 5) else _r["exp"])
            chart = snap.set_index("date")[["mv"]].assign(cum_pnl=cum_pnl)
            st.line_chart(chart, height=280)
            st.caption("正式库 data/vol.db · 口径铁律：每日快照落库，展示必差分；Theta 为年化口径（÷365=每日）")
        else:
            st.info("账本为空，点击上方按钮生成")
    except Exception as e:
        st.info(f"账本未就绪（{type(e).__name__}），点击上方按钮生成")

# ---------- Tab5 回测 ----------

with tab5:
    st.subheader("双引擎回测（IV 点代理 + 换向成本 30 元/次）")
    pct_states, ana_states, curves = run_backtest(v, df)
    fig = go.Figure()
    names = {"percentile": "分位数引擎", "analog": "类比引擎", "longvol": "买入持有长波(参照)"}
    colors = {"percentile": "#185FA5", "analog": "#0F6E56", "longvol": "#888780"}
    for k in curves:
        fig.add_trace(go.Scatter(x=df["date"], y=curves[k], mode="lines",
                                 name=names[k], line=dict(color=colors[k], width=2)))
    fig.update_layout(template="plotly_white", height=420,
                      yaxis_title="累计盈亏（元，代理）")
    st.plotly_chart(fig, use_container_width=True)
    rows = []
    for k in curves:
        nav = curves[k]
        rows.append({"策略": names[k], "累计盈亏": f"{nav[-1]:+.0f}",
                     "最大回撤": f"{max_drawdown(nav):.0f}"})
    st.dataframe(pd.DataFrame(rows), use_container_width=True)
    st.caption("可交易日约 " + str(len(df) - WINDOW) + " 个，仅覆盖单一 regime；结论待样本 ≥1 年后再下。")

# ---------- Tab6 模拟盘（§12，M6） ----------

with tab6:
    st.subheader("模拟盘 · 练手场（虚拟资金，与真实账户物理隔离，铁律 10）")
    st.caption("诚实声明（§12.5）：模拟盘成交假设偏乐观（无排队、无部分成交）；回放期权价是收盘截面 BS 合成价；"
               "**模拟盈利不构成实盘盈利预期**。跟随建议账户（自动镜像）按稳健档跟随系统建议，与自由账户同场竞技。")
    try:
        from api.services import create_sim_account, create_sim_order, run_sim_replay
        from db.session import make_engine, make_session

        def _sim_session():
            SM, _ = make_session(make_engine(f"sqlite:///{DB_PATH}"))
            return SM()

        import sqlite3 as _sq6
        _con = _sq6.connect(DB_PATH)
        accts = pd.read_sql_query(
            "SELECT id, name, cash0, auto_mirror, status FROM sim_account ORDER BY id", _con)
        fills_db = pd.read_sql_query(
            "SELECT f.id, o.account_id, f.filled_date, f.cp, f.strike, f.side, f.qty, "
            "f.fill_px, f.cost FROM sim_fill f JOIN sim_order o ON o.id=f.order_id "
            "ORDER BY f.id DESC LIMIT 50", _con)
        _panel_max = _con.execute("SELECT MAX(date) FROM daily_metric").fetchone()[0]
        _panel_min = _con.execute("SELECT MIN(date) FROM daily_metric").fetchone()[0]
        _con.close()

        # ---- 账户 ----
        st.markdown(f"**虚拟账户（{len(accts)}）**")
        if len(accts):
            st.dataframe(accts.rename(columns={
                "id": "ID", "name": "账户名", "cash0": "初始资金(元)",
                "auto_mirror": "跟随建议", "status": "状态"}),
                use_container_width=True, height=140)
        c1, c2, c3 = st.columns([2, 1, 1])
        with st.form("sim_new_account", border=True):
            _na = st.text_input("新账户名", value="练习账户", key="sim_acct_name")
            _nc = st.number_input("初始资金（元）", min_value=10000.0, value=100000.0,
                                  step=10000.0, key="sim_acct_cash")
            _nm = st.checkbox("跟随建议（自动镜像，稳健档）", value=False, key="sim_acct_mirror")
            if st.form_submit_button("➕ 创建账户", use_container_width=True):
                s = _sim_session()
                try:
                    a = create_sim_account(s, _na.strip() or "练习账户", float(_nc), bool(_nm))
                    st.success(f"已创建账户 #{a.id} {a.name}")
                    s.close()
                    st.rerun()
                except Exception as e:                 # noqa: BLE001
                    s.close()
                    st.error(f"创建失败：{e}")
        st.divider()

        # ---- 下单 ----
        st.markdown("**下单（自由 / 模板一键建仓）** —— 信息墙：只能用已有行情截面之日提交")
        if len(accts) == 0:
            st.info("先创建一个虚拟账户")
        else:
            with st.form("sim_new_order", border=True):
                _acc_map = {f"#{r['id']} {r['name']}": int(r["id"]) for _, r in accts.iterrows()}
                o1, o2, o3, o4 = st.columns([2, 2, 1, 1])
                _acc = o1.selectbox("账户", list(_acc_map), key="sim_ord_acct")
                _tpl = o2.selectbox("策略模板", ["buy_straddle", "sell_straddle", "sell_strangle"],
                                    format_func={"buy_straddle": "买入跨式（做多波动率）",
                                                 "sell_straddle": "卖出跨式（收权利金）",
                                                 "sell_strangle": "卖出宽跨式（虚值收权利金）"}.__getitem__,
                                    key="sim_ord_tpl")
                _qty = o3.number_input("张数", min_value=1, value=1, step=1, key="sim_ord_qty")
                _sub = o4.date_input("提交日（须有截面）",
                                     value=pd.Timestamp(_panel_max).date() if _panel_max else None,
                                     key="sim_ord_sub")
                _exp = st.date_input("到期日", value=None, min_value=_sub or None, key="sim_ord_exp")
                if st.form_submit_button("📤 提交订单", use_container_width=True):
                    s = _sim_session()
                    try:
                        o = create_sim_order(s, _acc_map[_acc], _sub, _exp,
                                             tpl_id=_tpl, qty=int(_qty))
                        st.success(f"订单 #{o.id} 已挂出（pending，{len(o.legs_json)} 腿）")
                        s.close()
                        st.rerun()
                    except LookupError as e:
                        s.close()
                        st.error(f"❌ {e}")
                    except ValueError as e:
                        s.close()
                        st.error(f"❌ {e}")
        st.divider()

        # ---- 回放 ----
        st.markdown("**回放（时间压缩）** —— 日频收盘截面撮合；面板不足 2 日拒绝（fail-closed）")
        r1, r2, r3 = st.columns([1, 1, 2])
        _from = r1.date_input("起", value=pd.Timestamp(_panel_min).date() if _panel_min else None,
                              key="sim_rp_from")
        _to = r2.date_input("止", value=pd.Timestamp(_panel_max).date() if _panel_max else None,
                            key="sim_rp_to")
        if r3.button("▶️ 运行回放", use_container_width=True, key="sim_rp_btn"):
            s = _sim_session()
            try:
                res = run_sim_replay(s, _from, _to)
                s.close()
                st.toast(f"回放完成：{res['days']} 个交易日，{len(res['fills'])} 笔成交")
                # 净值对比图（红涨绿跌：账户线用主题色，基准灰）
                fig = go.Figure()
                _pal = ["#D85A30", "#185FA5", "#0F6E56", "#8A5FBF", "#B8860B"]
                for i, (aid_str, curve) in enumerate(res["nav"].items()):
                    _aid = int(aid_str)
                    _nm = accts.loc[accts["id"] == _aid, "name"].iloc[0] if len(accts) else f"账户{_aid}"
                    fig.add_trace(go.Scatter(x=[c[0] for c in curve], y=[c[1] for c in curve],
                                             mode="lines+markers", name=f"{_nm}",
                                             line=dict(color=_pal[i % len(_pal)], width=2)))
                if res["benchmark"]:
                    fig.add_trace(go.Scatter(x=[c[0] for c in res["benchmark"]],
                                             y=[c[1] for c in res["benchmark"]],
                                             mode="lines", name="买入持有标的(基准)",
                                             line=dict(color="#888780", width=1.5, dash="dash")))
                fig.update_layout(title=f"净值对比（{_from} ~ {_to}）", template="plotly_white",
                                  height=380, yaxis_title="净值（元）")
                st.plotly_chart(fig, use_container_width=True)
                # 事件与成交
                if res["events"]:
                    for ev in res["events"]:
                        st.warning(f"📌 {ev['date']} IV 单日跳变 {ev['from']*100:.1f}% → {ev['to']*100:.1f}%"
                                   f"（关键事件，复盘重点看这天）")
                if res["fills"]:
                    st.dataframe(pd.DataFrame(res["fills"]).rename(columns={
                        "date": "成交日", "cp": "类型", "strike": "行权价", "side": "方向",
                        "qty": "张数", "fill_px": "成交价", "cost": "费用(元)"}),
                        use_container_width=True, height=200)
                else:
                    st.info("区间内无成交（订单提交日在区间外 / 已成交 / 已过期作废）")
            except RuntimeError as e:
                s.close()
                st.error(f"❌ {e}")
            except Exception as e:                         # noqa: BLE001
                s.close()
                st.error(f"回放失败：{e}")

        # ---- 成交记录 ----
        if len(fills_db):
            st.divider()
            st.markdown("**最近成交（50 条）**")
            st.dataframe(fills_db.rename(columns={
                "id": "回报ID", "account_id": "账户", "filled_date": "成交日", "cp": "类型",
                "strike": "行权价", "side": "方向", "qty": "张数",
                "fill_px": "成交价", "cost": "费用(元)"}), use_container_width=True, height=200)
        st.caption("⏳ 待接入：SSE 流式回放（§12.5）、错单本（亏损超阈自动收录三段复盘）、"
                   "归因瀑布抽屉。真实数据回放需日线任务累积 ≥2 个交易截面。")
    except Exception as e:                                 # noqa: BLE001 —— 模拟盘故障不阻断其他 Tab
        st.warning(f"模拟盘模块未就绪（{type(e).__name__}: {e}）——检查 sim 表是否已迁移（alembic upgrade head）")
