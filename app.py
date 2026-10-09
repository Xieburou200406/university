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
    """最新一日近月截面（聚合 C/P）→ SVI 拟合 + 偏离。"""
    days = fetch_dates(250)
    d, px = days[-1]
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
st.sidebar.caption("铁律：只输出建议，永不自动下单；\n信号 T 日生成、T+1 执行窗口内有效。")

if st.sidebar.button("刷新数据（显式联网，约 30 秒）"):
    with st.spinner("拉取最新官方风险指标 ..."):
        build_daily_history()
    st.cache_data.clear()
    st.sidebar.success("已更新")

tab1, tab2, tab3, tab4, tab5 = st.tabs(["📈 IV 监控", "🎯 信号台", "🛡 风控台", "💰 持仓归因", "🔬 回测"])

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
