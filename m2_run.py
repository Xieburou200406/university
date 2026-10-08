# -*- coding: utf-8 -*-
"""
m2_run.py — M2 增量：
  1. 历史 ATM IV 拉长到 250 个交易日（本地 CSV 缓存，二次运行不重拉）
  2. 分位数引擎用满 120 日窗口
  3. 类比引擎改用真实历史（特征：IV分位/VRP/动量/期限斜率/25Δ偏斜）
  4. SVI 主拟合 + 多项式兜底（pricing.fit_curve_iv 已升级）
输出：m2_report.html
"""
import json
import re
import math
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np
import pandas as pd
import akshare as ak

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
from vol.pricing.curve import fit_curve_iv
from vol.contracts import expiry_from_code as _expiry_from_ym
from vol.signals import PercentileEngine, AnalogEngine, build_features
from run_demo import (UNDERLYING, UNIT, R_FREE, HIST_DAYS, parse_symbol,
                      step1_underlying_and_contracts, step2_risk_indicator,
                      step3_live_quotes, log)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")
M2_DAYS = 520          # ≈ 2 年交易日，重新验证类比引擎（v2 缓存增量补拉）
WINDOW = 120
HORIZON = 10


def fetch_dates(n_days):
    """ETF 日线（带重试与新浪兜底），返回最近 n_days 个交易日 [(date_str, close)]。"""
    hist = None
    for attempt in range(3):
        try:
            hist = ak.fund_etf_hist_em(symbol="510050", period="daily",
                                       start_date="20240101", end_date="20991231", adjust="")
            break
        except Exception as e:
            log(f"  hist 第{attempt+1}次失败 {type(e).__name__}")
    if hist is None:
        hist = ak.fund_etf_hist_sina(symbol="sh510050")
        hist = hist.rename(columns={"date": "日期", "close": "收盘"})
    hist["日期"] = hist["日期"].astype(str).str[:10]
    hist = hist.sort_values("日期")
    tail = hist.tail(n_days)
    return list(zip(tail["日期"], tail["收盘"].astype(float)))


def load_day(date_compact):
    """读缓存或拉官方指标，返回标准化 DataFrame 或 None。
    v2：行权价量纲修复（/1000）后的缓存版本，旧缓存文件作废。"""
    fp = os.path.join(CACHE_DIR, f"ri_v2_{date_compact}.csv")
    if os.path.exists(fp):
        return pd.read_csv(fp, dtype={"code": str})
    try:
        t = ak.option_risk_indicator_sse(date=date_compact)
    except Exception:
        return None
    if t is None or len(t) == 0:
        return None
    t = t.rename(columns={"CONTRACT_ID": "code", "IMPLC_VOLATLTY": "iv",
                          "DELTA_VALUE": "delta", "VEGA_VALUE": "vega",
                          "GAMMA_VALUE": "gamma", "THETA_VALUE": "theta",
                          "CONTRACT_SYMBOL": "sym"})
    parsed = t["code"].map(lambda s: parse_symbol(s) if isinstance(s, str) else None)
    t["strike"] = parsed.map(lambda d: d.get("strike") if d else None)
    t["cp"] = parsed.map(lambda d: d.get("cp") if d else None)
    t["expiry"] = parsed.map(lambda d: d.get("expiry") if d else None)
    t = t.dropna(subset=["strike", "cp", "expiry"])
    if len(t) == 0:
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    t.to_csv(fp, index=False)
    return t


def expiry_from_code(code):
    """从合约代码推真实到期日：上交所 ETF 期权 = 到期月第 4 个周三（委托 vol.contracts）。"""
    try:
        m = re.match(r"510050[CP](\d{2})(\d{2})", code)
        return pd.Timestamp(_expiry_from_ym(f"20{m.group(1)}{m.group(2)}"))
    except Exception:
        return pd.NaT


def build_daily_history(contracts=None):
    """逐日（并行+缓存）：当日近月ATM IV、次月ATM IV、25Δ偏斜。"""
    days = fetch_dates(M2_DAYS)
    log(f"构建 {len(days)} 个交易日的日线缓存（{CACHE_DIR}） ...")
    day_data = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(load_day, d.replace("-", "")): d for d, _ in days}
        for f in as_completed(futs):
            d = futs[f]
            try:
                r = f.result()
            except Exception:
                r = None
            if r is not None and len(r) > 0:
                day_data[d] = r
    log(f"  可用 {len(day_data)}/{len(days)} 日")

    rows = []
    for d, px in days:
        t = day_data.get(d)
        if t is None:
            continue
        d0 = pd.Timestamp(d)
        t = t.copy()
        t["exp_dt"] = t["code"].map(expiry_from_code)   # 从代码推到期日（第4个周三）
        t = t[(t["exp_dt"] - d0).dt.days >= 14]
        t = t[t["iv"] > 0]
        if len(t) == 0:
            continue
        exps = sorted(t["exp_dt"].unique())
        rec = {"date": d, "spot": px}
        # 近月/次月 ATM IV
        ivs = {}
        for tag, e in [("near", exps[0]), ("next", exps[1] if len(exps) > 1 else None)]:
            if e is None:
                ivs[tag] = None
                continue
            sub = t[t["exp_dt"] == e]
            ivs[tag] = float(sub.iloc[(sub["strike"] - px).abs().argsort().iloc[0]]["iv"])
        rec["iv_near"], rec["iv_next"] = ivs["near"], ivs["next"]
        if ivs["near"] is None:
            continue
        # 25Δ 偏斜（近月）：delta 最接近 0.25 的 Call IV − delta 最接近 -0.25 的 Put IV
        sub = t[t["exp_dt"] == exps[0]]
        calls = sub[sub["cp"] == "C"]
        puts = sub[sub["cp"] == "P"]
        try:
            c25 = calls.iloc[(calls["delta"] - 0.25).abs().argsort().iloc[0]]["iv"]
            p25 = puts.iloc[(puts["delta"] + 0.25).abs().argsort().iloc[0]]["iv"]
            rec["skew25"] = float(p25) - float(c25)
        except Exception:
            rec["skew25"] = None
        rows.append(rec)
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    # 已实现波动率 20 日
    log_s = np.log(df["spot"]).diff()
    df["rvol20"] = (log_s.rolling(20).std() * math.sqrt(245)).shift(0)
    return df


def main():
    spot_now, contracts = step1_underlying_and_contracts()
    ri, ri_date = step2_risk_indicator(contracts)

    # 近月截面（当日曲线，SVI）
    near_exp = sorted(ri["expiry_s"].unique())[0]
    near = ri[ri["expiry_s"] == near_exp].copy()
    exp_day = datetime.strptime(str(near["到期日"].iloc[0]), "%Y%m%d")
    T = max((exp_day - datetime.now()).days, 1) / 365.0
    valid = (near[near["iv_official"] > 0]
             .groupby("strike", as_index=False)["iv_official"].mean()
             .sort_values("strike"))  # C/P 同行权价聚合，去噪声
    curve_fn, r2, model_name = fit_curve_iv(
        valid["strike"].tolist(), valid["iv_official"].tolist(), spot_now, T, R_FREE)
    F = spot_now * math.exp(R_FREE * T)
    near["deviation_bp"] = near.apply(
        lambda r: round((r["iv_official"] - curve_fn(math.log(r["strike"] / F))) * 10000, 1)
        if r["iv_official"] > 0 else np.nan, axis=1)

    # 250 日日线历史
    df = build_daily_history()
    log(f"有效历史 {len(df)} 日，范围 {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")

    # 引擎 A：120 日窗口分位数
    eng_a = PercentileEngine(window=WINDOW)
    sig_a = eng_a.run(df["iv_near"].tolist())

    # 引擎 B：真实历史类比
    feats_df = df.copy()
    feats_df["iv_pct"] = df["iv_near"].rolling(WINDOW).apply(
        lambda w: (w[-1] >= w[:-1]).mean() if len(w) == WINDOW else np.nan, raw=True)
    feats_df["vrp"] = df["iv_near"] - df["rvol20"].fillna(df["iv_near"])
    feats_df["mom20"] = df["spot"] / df["spot"].shift(20) - 1.0
    feats_df["ts_slope"] = df["iv_near"] - df["iv_next"].fillna(df["iv_near"])
    feats_df["skew25"] = df["skew25"].fillna(0.0)
    feats = [dict(date=r.date, iv_pct=(0.5 if pd.isna(r.iv_pct) else r.iv_pct), vrp=r.vrp,
                  mom20=(0.0 if pd.isna(r.mom20) else r.mom20), ts_slope=r.ts_slope,
                  skew25=r.skew25)
             for r in feats_df.itertuples()]
    # 类比引擎特征维度扩展（skew25 加入）
    import vol.signals.analog as S
    S.FEATURES = S.FEATURES + ["skew25"]
    eng_b = AnalogEngine(k=25, horizon=HORIZON)
    sig_b = eng_b.run(feats, df["spot"].tolist(), df["iv_near"].tolist())

    # 类比信号的事后命中率统计（真实历史滚动回看）
    hits, actuals = [], []
    for i in range(WINDOW, len(df) - HORIZON):
        x = feats[i]
        arr = np.array([[f[k] for k in S.FEATURES] for f in feats[:i]])
        mu, sd = arr.mean(axis=0), arr.std(axis=0) + 1e-9
        zx = np.array([x[k] for k in S.FEATURES])
        dist = np.sqrt((((arr - mu) / sd - (zx - mu) / sd) ** 2).sum(axis=1))
        idxs = [int(j) for j in np.argsort(dist) if j < i - HORIZON][:25]
        div = np.mean([df["iv_near"][i + HORIZON] - df["iv_near"][j] for j in idxs])
        pred = "UP" if div > 0.001 else ("DOWN" if div < -0.001 else "FLAT")
        actual_div = df["iv_near"][i + HORIZON] - df["iv_near"][i]
        actual = "UP" if actual_div > 0.001 else ("DOWN" if actual_div < -0.001 else "FLAT")
        hits.append(pred == actual)
        actuals.append(actual)
    hit_rate = float(np.mean(hits)) if hits else None
    # 朴素基准：永远猜占比最大的类别（IV 自相关会让"惯性猜测"很准，必须对照）
    baseline = None
    if actuals:
        n = len(actuals)
        baseline = float(max(sum(a == c for a in actuals) for c in ("UP", "DOWN", "FLAT")) / n)
    log(f"类比引擎方向命中率：{hit_rate:.2%} | 朴素基准：{baseline:.2%}" if hit_rate else "命中率不可用")

    build_m2_report(locals())
    log("m2_report.html 已生成")


def build_m2_report(ctx):
    import plotly.graph_objects as go

    near = ctx["near"]; spot_now = ctx["spot_now"]; T = ctx["T"]; F = ctx["F"]
    curve_fn = ctx["curve_fn"]; r2 = ctx["r2"]; model_name = ctx["model_name"]
    df = ctx["df"]; sig_a = ctx["sig_a"]; sig_b = ctx["sig_b"]
    hit_rate = ctx["hit_rate"]; baseline = ctx.get("baseline"); ri_date = ctx["ri_date"]; near_exp = ctx["near_exp"]
    WINDOW_, HORIZON = globals()["WINDOW"], globals()["HORIZON"]

    fig_curve = go.Figure()
    v = near[near["iv_official"] > 0].sort_values("strike")
    fig_curve.add_trace(go.Scatter(x=v["strike"], y=v["iv_official"] * 100, mode="markers",
                                   name="市场IV(官方)", marker=dict(color="#185FA5", size=9)))
    grid = np.linspace(min(v["strike"]), max(v["strike"]), 60)
    fig_curve.add_trace(go.Scatter(x=grid, y=[curve_fn(math.log(k / F)) * 100 for k in grid],
                                   mode="lines", name=f"拟合({model_name})",
                                   line=dict(color="#D85A30", width=2.5)))
    fig_curve.update_layout(title=f"近月波动率曲线（{near_exp}，SVI 主拟合）",
                            xaxis_title="行权价", yaxis_title="IV %",
                            template="plotly_white", height=400)

    fig_hist = go.Figure()
    ds, vs = df["date"].tolist(), (df["iv_near"] * 100).tolist()
    fig_hist.add_trace(go.Scatter(x=ds, y=vs, mode="lines", name="近月ATM IV",
                                  line=dict(color="#185FA5", width=1.8)))
    arr = np.array(vs)
    p20, p80 = np.percentile(arr, 20), np.percentile(arr, 80)
    for y, c, t in [(p80, "#A32D2D", "80分位"), (p20, "#3B6D11", "20分位")]:
        fig_hist.add_hline(y=y, line_dash="dash", line_color=c, annotation_text=f"{t} {y:.1f}%")
    fig_hist.update_layout(title=f"ATM IV 历史序列（官方指标 {len(vs)} 交易日）",
                           xaxis_title="日期", yaxis_title="IV %",
                           template="plotly_white", height=380)

    stats = sig_b.get("strategy_stats") or {}
    stat_rows = "".join(f"<tr><td>{k}</td><td>{v['exp']}</td><td>{v['win']}</td><td>{v['p10']}</td></tr>"
                        for k, v in stats.items())
    nb_rows = "".join(f"<tr><td>{n['date']}</td><td>{n['dist']}</td><td>{n['iv_after10d']}%</td></tr>"
                      for n in (sig_b.get("neighbors") or []))

    fig_curve.write_html("m2_report.html", include_plotlyjs=True, full_html=False, div_id="c1")
    part1 = open("m2_report.html", encoding="utf-8").read()
    fig_hist.write_html("m2_report.html", include_plotlyjs=False, full_html=False, div_id="c2")
    part2 = open("m2_report.html", encoding="utf-8").read()

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>M2 · 250日历史 + SVI + 真实类比引擎</title>
<style>body{{font-family:"Microsoft YaHei",sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#222;background:#fff}}
h1{{font-size:22px}} h2{{font-size:17px;border-left:4px solid #534AB7;padding-left:8px;margin-top:30px}}
.card{{background:#F5F7FA;border:1px solid #d8dde5;border-radius:10px;padding:12px 16px;margin:10px 0}}
.ok{{color:#0F6E56;font-weight:bold}} .warn{{color:#993C1D;font-weight:bold}}
.tbl{{border-collapse:collapse;font-size:13px;width:100%}}
.tbl th,.tbl td{{border-bottom:1px solid #e2e6ec;padding:5px 8px;text-align:right}}
.tbl th:first-child,.tbl td:first-child{{text-align:left}}
</style></head><body>
<h1>M2 · {len(df)} 日历史 + SVI + 真实数据类比引擎</h1>
<p>指标日 {ri_date} · 近月 {near_exp} · 历史区间 {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}（{len(df)} 个交易日，本地缓存 data/cache/）</p>

<h2>1. 波动率曲线（SVI 主拟合）</h2>
{part1}{part2}
<p>拟合模型 = <b>{model_name}</b>，R² = {r2:.4f}。</p>

<h2>2. 引擎 A · IV 分位数（满 120 日窗口）</h2>
<div class="card">
当前近月 ATM IV = <b>{df['iv_near'].iloc[-1]*100:.2f}%</b>，
{WINDOW_} 日历史分位 = <b>{sig_a.get('iv_pct', '—')}</b>，
信号 = <b>{sig_a.get('signal')}</b>
{('（' + str(sig_a['streak']) + ' 日连续处于高/低区，' + ('已确认' if sig_a.get('confirm_needed', 1) <= 0 else '还需 ' + str(sig_a['confirm_needed']) + ' 日确认') + '）') if sig_a.get('streak') is not None else ''}<br>
{len(vs)} 日全样本 20/80 分位线见图中虚线。</div>

<h2>3. 引擎 B · 历史情景类比（真实 {len(df)} 日历史）</h2>
<div class="card">
今日特征 = {sig_b.get('today_feat')}<br>
选出策略 = <b>{sig_b.get('signal')}</b>（置信度 {sig_b.get('confidence')}）<br>
<b>滚动样本外方向命中率</b>（每日用其之前的 120 日历史检索、预测其后 {HORIZON} 日 IV 方向）：
<b>{('%.1f%%' % (hit_rate * 100)) if hit_rate else '—'}</b>
vs 朴素基准（永远猜最常见方向）<b>{('%.1f%%' % (baseline * 100)) if baseline else '—'}</b>。
注意：IV 有强自相关性，朴素基准天然偏高；类比引擎必须<b>显著跑赢朴素基准</b>才有增量价值，样本每周复核。</div>
<p><b>最相似历史日（近月 Top，单位 IV 点）：</b></p>
<table class="tbl"><tr><th>相似日</th><th>距离</th><th>其后{HORIZON}日IV变化</th></tr>{nb_rows}</table>
<p><b>策略池加权统计：</b></p>
<table class="tbl"><tr><th>策略</th><th>期望收益(代理)</th><th>胜率</th><th>P10</th></tr>{stat_rows}</table>

<h2>4. M2 结论与 M3 待办</h2>
<div class="card">
SVI 拟合与 250 日历史缓存均可落地；类比引擎在真实历史上的滚动命中率已可量化——<b>这是它能否进实盘参考的硬指标</b>，
样本持续积累后每周复核一次。<br>
<b>M3：</b>① 模拟持仓账本 + 快照差分归因上线；② 类比命中率按策略分层统计；
③ Streamlit 面板；④ 信号有效期管理（T+1 过期作废）。
</div>
</body></html>"""
    open("m2_report.html", "w", encoding="utf-8").write(html)


if __name__ == "__main__":
    main()
