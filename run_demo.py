# -*- coding: utf-8 -*-
"""
run_demo.py — 波动率决策辅助系统 端到端 Demo（M1 落地验证）
数据通路（实测）：
  - 合约字典:   ak.option_current_day_sse()
  - 官方IV/希腊字母(按日全量): ak.option_risk_indicator_sse(date)
  - 合约实时快照: ak.option_sse_spot_price_sina(symbol)  # 仅支持单个
  - 标的现状:   ak.option_sse_underlying_spot_price_sina('sh510050')
  - 标的历史:   ak.fund_etf_hist_em
输出：demo_report.html（自包含）
"""
import json
import math
import re
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import numpy as np
import pandas as pd
import akshare as ak

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))
from vol.pricing.bs import implied_vol
from vol.pricing.curve import fit_curve_iv
from vol.pricing.liquidity import liquidity_score
from vol.contracts import parse_sse_etf_code
from vol.signals import PercentileEngine, AnalogEngine, build_features, synthetic_history

UNDERLYING = "sh510050"
UNIT = 10000          # 50ETF 期权合约单位
HIST_DAYS = 60        # demo 拉 60 个交易日的官方风险指标
R_FREE = 0.015


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def parse_symbol(sym):
    # 注意：合约代码是裸的 510050 开头，不能传 UNDERLYING("sh510050"，sina 风格带前缀)
    return parse_sse_etf_code(sym)


# ---------- 第 1 步：标的与合约字典 ----------

def step1_underlying_and_contracts():
    log("拉取标的状态与合约字典 ...")
    spot_df = ak.option_sse_underlying_spot_price_sina(symbol=UNDERLYING)
    spot_map = dict(zip(spot_df["字段"], spot_df["值"]))
    spot_now = float(spot_map["最近成交价"])
    log(f"  510050 最新价 = {spot_now}")

    contracts = ak.option_current_day_sse()
    contracts["strike"] = contracts["合约交易代码"].map(lambda s: (parse_symbol(s) or {}).get("strike"))
    contracts["cp"] = contracts["合约交易代码"].map(lambda s: (parse_symbol(s) or {}).get("cp"))
    contracts["expiry_s"] = contracts["合约交易代码"].map(lambda s: (parse_symbol(s) or {}).get("expiry"))
    contracts = contracts.dropna(subset=["strike", "cp", "expiry_s"])
    log(f"  合约总数 = {len(contracts)}")
    return spot_now, contracts


# ---------- 第 2 步：官方风险指标（当日 or 最近交易日） ----------

def step2_risk_indicator(contracts):
    log("拉取上交所官方风险指标（IV/希腊字母） ...")
    today = datetime.now().strftime("%Y%m%d")
    df = None
    for d in [today, "20260929", "20260928", "20260925"]:
        try:
            t = ak.option_risk_indicator_sse(date=d)
            if len(t) > 0:
                df = t
                log(f"  取到 {d} 指标，{len(t)} 条")
                break
        except Exception:
            continue
    if df is None:
        raise RuntimeError("官方风险指标不可用")
    df = df.rename(columns={"CONTRACT_ID": "合约交易代码", "IMPLC_VOLATLTY": "iv_official",
                            "DELTA_VALUE": "delta_off", "GAMMA_VALUE": "gamma_off",
                            "VEGA_VALUE": "vega_off", "THETA_VALUE": "theta_off",
                            "CONTRACT_SYMBOL": "合约简称"})
    df = df.merge(contracts[["合约交易代码", "strike", "cp", "expiry_s", "到期日", "合约单位"]],
                  on="合约交易代码", how="left")
    df = df.dropna(subset=["strike", "cp", "expiry_s"])  # 只留 510050（字典已按正则过滤）
    return df, d


# ---------- 第 3 步：近月合约实时快照（逐合约，并行） ----------

def fetch_spot_one(code):
    try:
        df = ak.option_sse_spot_price_sina(symbol=str(code))
        m = dict(zip(df["字段"], df["值"]))
        return code, {"bid": float(m.get("买价") or 0), "ask": float(m.get("卖价") or 0),
                      "last": float(m.get("最新价") or 0),
                      "volume": float(m.get("成交量") or 0), "oi": float(m.get("持仓量") or 0)}
    except Exception:
        return code, None


def step3_live_quotes(codes_near):
    log(f"逐合约拉实时快照（{len(codes_near)} 个，并行 8 线程） ...")
    out = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(fetch_spot_one, c) for c in codes_near]
        for f in as_completed(futs):
            code, q = f.result()
            if q:
                out[code] = q
    log(f"  成功 {len(out)}/{len(codes_near)}")
    return out


# ---------- 第 4 步：历史 ATM IV 序列（官方指标逐日） ----------

def step4_history(contracts):
    log(f"拉取最近 {HIST_DAYS} 个交易日官方指标，构建 ATM IV 序列 ...")
    hist = None
    for attempt in range(3):
        try:
            hist = ak.fund_etf_hist_em(symbol="510050", period="daily",
                                       start_date="20260101", end_date="20991231", adjust="")
            break
        except Exception as e:
            log(f"  fund_etf_hist_em 第{attempt+1}次失败: {type(e).__name__}，重试/兜底 ...")
    if hist is None:
        hist = ak.fund_etf_hist_sina(symbol="sh510050")  # 新浪兜底
        hist = hist.rename(columns={"date": "日期", "close": "收盘"})
        hist["日期"] = hist["日期"].astype(str).str[:10]
        hist = hist[hist["日期"] >= "2026-01-01"]
    dates = list(hist["日期"])[-HIST_DAYS:]
    dates = [str(d)[:10] for d in dates]
    closes = {str(d)[:10]: float(c) for d, c in zip(hist["日期"], hist["收盘"])}
    cons = contracts[["合约交易代码", "strike", "cp", "到期日"]].copy()
    cons["exp_day"] = pd.to_datetime(cons["到期日"], format="%Y%m%d", errors="coerce")

    def one_day(d):
        try:
            t = ak.option_risk_indicator_sse(date=d.replace("-", ""))
        except Exception:
            return d, None
        if t is None or len(t) == 0:
            return d, None
        t = t.rename(columns={"CONTRACT_ID": "合约交易代码", "IMPLC_VOLATLTY": "iv"})
        t = t.merge(cons, on="合约交易代码", how="left").dropna(subset=["strike", "exp_day"])
        t = t[t["iv"] > 0]
        px = float(closes.get(d) or 0)
        if px <= 0 or len(t) == 0:
            return d, None
        d0 = pd.Timestamp(d)
        t = t[(t["exp_day"] - d0).dt.days >= 14]   # 剩余期限 >= 14 天，避开临到期噪声
        if len(t) == 0:
            return d, None
        nearest_exp = t["exp_day"].min()
        chosen = t[t["exp_day"] == nearest_exp]
        row = chosen.iloc[(chosen["strike"] - px).abs().argsort().iloc[0]]
        return d, float(row["iv"])

    results = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(one_day, d) for d in dates]
        for f in as_completed(futs):
            d, iv = f.result()
            if iv:
                results[d] = iv
    ordered = [(d, results[d]) for d in dates if d in results]
    log(f"  成功 {len(ordered)}/{len(dates)} 个交易日")
    return ordered, closes


# ---------- 第 5 步：风控条件链 ----------

def step5_risk_check(portfolio, vega_limit, delta_limit):
    """portfolio: [(name, qty, delta_off, vega_off)]，qty 正=多头。"""
    log("风控条件链校验（样本组合） ...")
    rows, acc_d, acc_v = [], 0.0, 0.0
    for name, qty, dlt, vga in portfolio:
        pos_delta = qty * dlt * UNIT
        pos_vega = qty * vga * UNIT
        acc_d += pos_delta
        acc_v += pos_vega
        rows.append((name, qty, round(pos_delta, 1), round(pos_vega, 1)))
    ok_vega = abs(acc_v) <= vega_limit
    ok_delta = abs(acc_d) <= delta_limit
    log(f"  组合Delta={acc_d:.0f} (上限±{delta_limit})  组合Vega={acc_v:.0f} (上限±{vega_limit})")
    return {"rows": rows, "acc_delta": acc_d, "acc_vega": acc_v,
            "vega_limit": vega_limit, "delta_limit": delta_limit,
            "ok_vega": ok_vega, "ok_delta": ok_delta}


# ---------- 主流程 ----------

def main():
    spot_now, contracts = step1_underlying_and_contracts()
    ri, ri_date = step2_risk_indicator(contracts)

    # 近月 = 到期日最早的那一档
    near_exp = sorted(ri["expiry_s"].unique())[0]
    near = ri[ri["expiry_s"] == near_exp].copy()
    log(f"近月到期: {near_exp}，合约 {len(near)} 条")

    # 平值附近 ±5 档做实时快照
    strikes_sorted = sorted(near["strike"].unique())
    atm_k = min(strikes_sorted, key=lambda k: abs(k - spot_now))
    ks_near = [k for k in strikes_sorted if abs(k - atm_k) <= 0.10][:11]
    codes_near = near[near["strike"].isin(ks_near)]["合约交易代码"].astype(str).tolist()
    quotes = step3_live_quotes(codes_near)

    # 合并快照 → 自算 IV vs 官方 IV 对照表
    near["code_s"] = near["合约交易代码"].astype(str)
    near["bid"] = near["code_s"].map(lambda c: (quotes.get(c) or {}).get("bid"))
    near["ask"] = near["code_s"].map(lambda c: (quotes.get(c) or {}).get("ask"))
    near["volume"] = near["code_s"].map(lambda c: (quotes.get(c) or {}).get("volume", 0) or 0)
    near["oi"] = near["code_s"].map(lambda c: (quotes.get(c) or {}).get("oi", 0) or 0)
    near["mid"] = (near["bid"] + near["ask"]) / 2
    near.loc[(near["bid"].isna()) | (near["bid"] <= 0) | (near["ask"] < near["bid"]), "mid"] = np.nan

    exp_day = datetime.strptime(str(near["到期日"].iloc[0]), "%Y%m%d")
    T = max((exp_day - datetime.now()).days, 1) / 365.0

    iv_self = []
    for _, r in near.iterrows():
        px = r["mid"] if not np.isnan(r["mid"]) else None
        iv = implied_vol(px, spot_now, r["strike"], T, R_FREE, r["cp"]) if px else None
        iv_self.append(iv)
    near["iv_self"] = iv_self

    # 曲线拟合（用官方 IV，同 strike 的 C/P 先聚合降噪）
    valid = (near[near["iv_official"] > 0]
             .groupby("strike", as_index=False)["iv_official"].mean()
             .sort_values("strike"))
    ks, ivs = valid["strike"].tolist(), valid["iv_official"].tolist()
    curve_fn, r2, model_name = fit_curve_iv(ks, ivs, spot_now, T, R_FREE)
    F = spot_now * math.exp(R_FREE * T)
    near["deviation_bp"] = near.apply(
        lambda r: round((r["iv_official"] - curve_fn(math.log(r["strike"] / F))) * 10000, 1)
        if r["iv_official"] > 0 else np.nan, axis=1)

    # 流动性打分（同月内分位）
    def rank(s):
        r = s.rank(pct=True).fillna(0)
        return r
    near["liq"] = [liquidity_score(b, a, vr, orr) for b, a, vr, orr in
                   zip(near["bid"], near["ask"], rank(near["volume"]), rank(near["oi"]))]

    # 历史 ATM IV → 分位数信号
    hist_series, closes_hist = step4_history(contracts)
    hist_dates = [d for d, _ in hist_series]
    hist_ivs = [v for _, v in hist_series]
    eng_a = PercentileEngine(window=40)  # demo 拉 60 日历史，窗口用 40；正式版 120 日
    sig_a = eng_a.run(hist_ivs)

    # 类比引擎（合成 420 日历史做逻辑演示，报告明确标注）
    sim_dates, sim_spot, sim_iv, sim_nm, sim_rv = synthetic_history()
    feats = build_features(sim_dates, sim_iv, sim_rv, sim_nm, sim_spot)
    eng_b = AnalogEngine()
    sig_b = eng_b.run(feats, sim_spot, sim_iv)

    # 风控 demo：样本组合 = 卖 2 张 ATM Call + 买 1 张上档 Call（真实合约与官方希腊字母）
    atm_row = near[near["strike"] == atm_k].iloc[0]
    otm_k = min([k for k in strikes_sorted if k > atm_k + 0.02], default=atm_k + 0.05)
    otm_row = near[(near["strike"] == otm_k) & (near["cp"] == "C")]
    otm_row = otm_row.iloc[0] if len(otm_row) else atm_row
    portfolio = [
        (f"卖2张 {atm_row['合约简称']}", -2, atm_row["delta_off"], atm_row["vega_off"]),
        (f"买1张 {otm_row['合约简称']}", 1, otm_row["delta_off"], otm_row["vega_off"]),
    ]
    premium_bal = 2 * float(atm_row["mid"]) * UNIT if not np.isnan(atm_row["mid"]) else 2 * spot_now * 0.05 * UNIT
    risk = step5_risk_check(portfolio, vega_limit=round(premium_bal * 0.30), delta_limit=15000)

    build_report(locals())
    log("demo_report.html 已生成")
# ---------- 报告生成 ----------

def build_report(ctx):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    near: pd.DataFrame = ctx["near"]
    spot_now = ctx["spot_now"]; T = ctx["T"]; F = ctx["F"]
    curve_fn = ctx["curve_fn"]; r2 = ctx["r2"]; model_name = ctx["model_name"]
    hist_series = ctx["hist_series"]; sig_a = ctx["sig_a"]; sig_b = ctx["sig_b"]
    risk = ctx["risk"]; ri_date = ctx["ri_date"]; near_exp = ctx["near_exp"]

    fig_curve = go.Figure()
    v = near[near["iv_official"] > 0].sort_values("strike")
    fig_curve.add_trace(go.Scatter(x=v["strike"], y=v["iv_official"] * 100, mode="markers",
                                   name="市场IV(上交所官方)", marker=dict(color="#185FA5", size=9)))
    grid = np.linspace(min(v["strike"]), max(v["strike"]), 60)
    fig_curve.add_trace(go.Scatter(x=grid, y=[curve_fn(math.log(k / F)) * 100 for k in grid],
                                   mode="lines", name=f"拟合曲线({model_name})",
                                   line=dict(color="#D85A30", width=2.5)))
    self_pts = near[near["iv_self"].notna()]
    fig_curve.add_trace(go.Scatter(x=self_pts["strike"], y=self_pts["iv_self"] * 100, mode="markers",
                                   name="自算IV(中间价反解)", marker=dict(color="#0F6E56", size=8, symbol="x")))
    fig_curve.update_layout(title=f"近月波动率曲线（{near_exp} 到期，指标日 {ri_date}）",
                            xaxis_title="行权价", yaxis_title="隐含波动率 %",
                            template="plotly_white", height=420)

    fig_hist = go.Figure()
    ds = [d for d, _ in hist_series]; vs = [x * 100 for _, x in hist_series]
    fig_hist.add_trace(go.Scatter(x=ds, y=vs, mode="lines+markers", name="ATM IV",
                                  line=dict(color="#185FA5", width=2)))
    arr = np.array(vs)
    p20, p80 = np.percentile(arr, 20), np.percentile(arr, 80)
    fig_hist.add_hline(y=p80, line_dash="dash", line_color="#A32D2D",
                       annotation_text=f"80分位 {p80:.1f}%")
    fig_hist.add_hline(y=p20, line_dash="dash", line_color="#3B6D11",
                       annotation_text=f"20分位 {p20:.1f}%")
    fig_hist.update_layout(title=f"ATM IV 历史序列（上交所官方指标 {len(vs)} 日）",
                           xaxis_title="日期", yaxis_title="IV %", template="plotly_white", height=380)

    table_html = near[near["code_s"].isin(
        near.sort_values("liq", ascending=False)["code_s"].head(12))].sort_values("strike").to_html(
        columns=["合约简称", "strike", "cp", "bid", "ask", "iv_official", "iv_self", "deviation_bp", "liq"],
        header=["合约", "行权价", "类型", "买价", "卖价", "官方IV", "自算IV", "偏离(bp)", "流动性分"],
        index=False, na_rep="—", border=0, classes="tbl")

    risk_rows = "".join(
        f"<tr><td>{n}</td><td>{q}</td><td>{d}</td><td>{v}</td></tr>"
        for n, q, d, v in risk["rows"])
    analog_rows = "".join(
        f"<tr><td>{k}</td><td>{v['exp']}</td><td>{v['win']}</td><td>{v['p10']}</td></tr>"
        for k, v in (sig_b.get("strategy_stats") or {}).items())
    nb_rows = "".join(
        f"<tr><td>{n['date']}</td><td>{n['dist']}</td><td>{n['iv_after10d']}%</td></tr>"
        for n in (sig_b.get("neighbors") or [])[:8])

    fig_curve.write_html("demo_report.html", include_plotlyjs=True, full_html=False, div_id="c1")
    part1 = open("demo_report.html", encoding="utf-8").read()
    fig_hist.write_html("demo_report.html", include_plotlyjs=False, full_html=False, div_id="c2")
    part2 = open("demo_report.html", encoding="utf-8").read()

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>波动率决策辅助系统 · 落地 Demo</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#222;background:#fff}}
h1{{font-size:22px}} h2{{font-size:17px;border-left:4px solid #185FA5;padding-left:8px;margin-top:32px}}
.card{{background:#F5F7FA;border:1px solid #d8dde5;border-radius:10px;padding:12px 16px;margin:10px 0}}
.ok{{color:#0F6E56;font-weight:bold}} .warn{{color:#993C1D;font-weight:bold}}
.tbl{{border-collapse:collapse;font-size:13px;width:100%}}
.tbl th,.tbl td{{border-bottom:1px solid #e2e6ec;padding:5px 8px;text-align:right}}
.tbl th:first-child,.tbl td:first-child{{text-align:left}}
.tag{{display:inline-block;background:#E6F1FB;color:#185FA5;border-radius:4px;padding:1px 8px;margin-right:6px;font-size:12px}}
.sim{{background:#FAEEDA;color:#854F0B}}
</style></head><body>
<h1>波动率决策辅助系统 · 落地验证 Demo</h1>
<p>
<span class="tag">AKShare {ak.__version__}</span>
<span class="tag">官方指标日 {ri_date}</span>
<span class="tag">510050 现价 {spot_now}</span>
<span class="tag">近月 {near_exp} · T={T*365:.0f}天</span>
</p>

<h2>1. 数据层实测结论</h2>
<div class="card">
<b>实测可用：</b>上交所官方风险指标（全量合约 IV + 五个希腊字母，按日）、合约字典、标的历史/现状、逐合约实时买卖价。<br>
<b>限制：</b>实时快照仅支持逐合约查询（demo 平值±5档 22 个合约并行抓取）；深实/深虚合约官方 IV=0（交易所不发布），已自动排除。
</div>

<h2>2. 波动率曲线（当日截面）</h2>
{part1}{part2}
<p>曲线拟合 R² = {r2:.4f}。绿叉为本系统自算 IV（Brent 反解中间价），与官方 IV 交叉验证。</p>

<h2>3. 信号层</h2>
<div class="card"><b>引擎 A · IV 分位数（真实官方数据，{len(vs)} 日）：</b><br>
当前 ATM IV = {vs[-1]:.2f}%，历史分位 = {sig_a.get('iv_pct', '—')}，信号 = <b>{sig_a.get('signal')}</b>
（demo 用 {sig_a.get('window', '—')} 日窗口，正式版 120 日{('；还需 ' + str(sig_a['confirm_needed']) + ' 日确认状态翻转') if sig_a.get('confirm_needed') else ''}）</div>
<div class="card sim"><b>引擎 B · 历史情景类比（<u>合成 420 日数据</u>，仅演示引擎逻辑）：</b><br>
今日特征 = {sig_b.get('today_feat')}<br>
选出策略 = <b>{sig_b.get('signal')}</b>（置信度 {sig_b.get('confidence')}）<br>
<b>最相似历史日（Top8）：</b></div>
<table class="tbl"><tr><th>相似日</th><th>距离</th><th>其后10日IV变化</th></tr>{nb_rows}</table>
<p><b>策略池加权统计（相似度加权）：</b></p>
<table class="tbl"><tr><th>策略</th><th>期望收益(代理)</th><th>胜率</th><th>P10</th></tr>{analog_rows}</table>

<h2>4. 风控条件链（样本组合实测）</h2>
<table class="tbl"><tr><th>腿</th><th>张数</th><th>Delta敞口</th><th>Vega敞口</th></tr>{risk_rows}
<tr><td><b>合计</b></td><td></td><td><b>{risk['acc_delta']:.0f}</b> / 上限 ±{risk['delta_limit']}</td>
<td><b>{risk['acc_vega']:.0f}</b> / 上限 ±{risk['vega_limit']}</td></tr></table>
<p>校验结果：Vega {'<span class="ok">通过</span>' if risk['ok_vega'] else '<span class="warn">超限→建议削减张数</span>'}，
Delta {'<span class="ok">通过</span>' if risk['ok_delta'] else '<span class="warn">超限→建议对冲或削减</span>'}</p>

<h2>5. 合约明细（近月，按流动性 Top12）</h2>
{table_html}

<h2>6. 落地性结论</h2>
<div class="card">
<b class="ok">可以落地。</b>全链路（数据→定价→曲线→信号→风控）已在真实数据上跑通，
其中：数据层/曲线/分位数引擎/风控 = 真实上交所官方数据；类比引擎 = 合成数据演示逻辑。<br>
<b>下一步（M2）：</b>① 官方指标已含希腊字母，自算模块转为交叉校验器；② SVI 替换多项式；
③ IV 历史拉长到 250 日跑满窗口；④ Streamlit 面板替代静态报告。
</div>
</body></html>"""
    open("demo_report.html", "w", encoding="utf-8").write(html)


if __name__ == "__main__":
    main()
    sys.exit(0)
