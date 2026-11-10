# -*- coding: utf-8 -*-
"""M8-④ 全量历史复现（2015-02-09 510050 期权上市起）。

流程：
1. m2_run.M2_DAYS=3000 复用 build_daily_history() 逐日并行拉缓存（断点续拉：
   load_day 只对缺缓存的日子触网；瞬时失败跑多轮补齐直到收敛）；
2. 复用 m3_run.analog_states + 分位数引擎做 walk-forward 方向命中率（铁律口径：
   WINDOW=120 / HORIZON=10 / T+1 生效 / |ΔIV|≤0.001 跳过）；
3. 按年度聚合 vs 朴素基准，重点核查 2020（疫情）与 2024（924 行情）极端段；
4. 产出 docs/报告-M8-④-全量历史复现.md + 交互图 docs/report-m8-4-chart.html。

用法：python m8_run.py            （全流程，缓存已存在时秒级）
      python m8_run.py --fetch    （仅补拉缓存，不跑分析）
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import math
import numpy as np
import pandas as pd

import m2_run
from m2_run import log, load_day, fetch_dates, expiry_from_code, as_completed, ThreadPoolExecutor
from m3_run import WINDOW, HORIZON, percentile_states, analog_states

FULL_DAYS = 3000                 # 2015-02 ~ 2026-10 ≈ 2840 交易日，留余量
FOCUS_YEARS = ("2020", "2024")   # 极端行情核查年
REPORT_MD = os.path.join(m2_run.ROOT, "docs", "报告-M8-④-全量历史复现.md")
REPORT_HTML = os.path.join(m2_run.ROOT, "docs", "report-m8-4-chart.html")


def build_full_panel(rounds=3):
    """全量面板：多轮补拉直到收敛（load_day 缺缓存才触网 → 天然断点续拉）。"""
    days = fetch_dates(FULL_DAYS)
    log(f"M8-④ 全量目标 {len(days)} 交易日（{days[0][0]} ~ {days[-1][0]}）")
    day_data = {}
    for rd in range(1, rounds + 1):
        todo = [d for d, _ in days if d not in day_data]
        if not todo:
            break
        log(f"  第 {rd} 轮：{len(todo)} 日待拉 ...")
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(load_day, d.replace("-", "")): d for d in todo}
            done = 0
            for f in as_completed(futs):
                d = futs[f]
                try:
                    r = f.result()
                except Exception:
                    r = None
                if r is not None and len(r) > 0:
                    day_data[d] = r
                done += 1
                if done % 200 == 0:
                    log(f"    进度 {done}/{len(todo)}（累计可用 {len(day_data)}）")
        avail = sum(1 for d, _ in days if d in day_data)
        log(f"  第 {rd} 轮结束：可用 {avail}/{len(days)}")
    return days, day_data


def assemble_panel(days, day_data):
    """与 m2_run.build_daily_history 相同的组装口径（近月/次月 ATM IV、25Δ 偏斜、rvol20）。"""
    rows = []
    for d, px in days:
        t = day_data.get(d)
        if t is None:
            continue
        d0 = pd.Timestamp(d)
        t = t.copy()
        t["exp_dt"] = t["code"].map(expiry_from_code)
        t = t[(t["exp_dt"] - d0).dt.days >= 14]
        t = t[t["iv"] > 0]
        if len(t) == 0:
            continue
        exps = sorted(t["exp_dt"].unique())
        rec = {"date": d, "spot": px}
        e0, e1 = exps[0], (exps[1] if len(exps) > 1 else None)
        rec["iv_near"] = float(t[t["exp_dt"] == e0].iloc[
            (t[t["exp_dt"] == e0]["strike"] - px).abs().argsort().iloc[0]]["iv"])
        rec["iv_next"] = (float(t[t["exp_dt"] == e1].iloc[
            (t[t["exp_dt"] == e1]["strike"] - px).abs().argsort().iloc[0]]["iv"])
            if e1 is not None else None)
        sub = t[t["exp_dt"] == e0]
        try:
            calls, puts = sub[sub["cp"] == "C"], sub[sub["cp"] == "P"]
            c25 = calls.iloc[(calls["delta"] - 0.25).abs().argsort().iloc[0]]["iv"]
            p25 = puts.iloc[(puts["delta"] + 0.25).abs().argsort().iloc[0]]["iv"]
            rec["skew25"] = float(p25) - float(c25)
        except Exception:
            rec["skew25"] = None
        rows.append(rec)
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    log_s = np.log(df["spot"]).diff()
    df["rvol20"] = log_s.rolling(20).std() * math.sqrt(245)
    return df


def walk_forward_yearly(df):
    """walk-forward 方向命中率按年聚合：类比 vs 分位数 vs 朴素基准。"""
    iv = df["iv_near"].tolist()
    n = len(iv)
    med = float(np.median(iv))
    pct = percentile_states(iv)
    ana = analog_states(df)

    agg = {}   # year -> dict
    def _slot(year):
        return agg.setdefault(year, {
            "n": 0, "up_n": 0, "ana_n": 0, "ana_hit": 0, "pct_n": 0, "pct_hit": 0,
            "hi_n": 0, "hi_hit": 0, "lo_n": 0, "lo_hit": 0})

    for i in range(WINDOW, n - HORIZON):
        year = str(df["date"].iloc[i])[:4]
        a = _slot(year)
        actual = iv[i + HORIZON] - iv[i]
        if abs(actual) > 0.001:
            up = actual > 0
            a["n"] += 1
            a["up_n"] += int(up)
            # 类比引擎
            if ana[i] != 0:
                h = (ana[i] > 0) == up
                a["ana_n"] += 1
                a["ana_hit"] += int(h)
                bucket = "hi" if iv[i] > med else "lo"
                a[f"{bucket}_n"] += 1
                a[f"{bucket}_hit"] += int(h)
            # 分位数引擎
            if pct[i - 1] in ("LONG_VOL", "SHORT_VOL"):
                h = (pct[i - 1] == "LONG_VOL") == up
                a["pct_n"] += 1
                a["pct_hit"] += int(h)

    out = []
    for year in sorted(agg):
        a = agg[year]
        up_rate = a["up_n"] / a["n"] if a["n"] else 0.5
        naive = max(up_rate, 1 - up_rate)
        out.append({
            "year": year,
            "samples": a["ana_n"],
            "ana_hit": (a["ana_hit"] / a["ana_n"]) if a["ana_n"] else float("nan"),
            "pct_hit": (a["pct_hit"] / a["pct_n"]) if a["pct_n"] else float("nan"),
            "naive": naive,
            "hi_hit": (a["hi_hit"] / a["hi_n"]) if a["hi_n"] else float("nan"),
            "lo_hit": (a["lo_hit"] / a["lo_n"]) if a["lo_n"] else float("nan"),
        })
    return pd.DataFrame(out)


def yearly_focus(df, years=FOCUS_YEARS):
    """极端行情月度明细：指定年份逐月样本数与命中率。"""
    iv = df["iv_near"].tolist()
    n = len(iv)
    ana = analog_states(df)
    rows = []
    for i in range(WINDOW, n - HORIZON):
        d = str(df["date"].iloc[i])
        if d[:4] not in years:
            continue
        actual = iv[i + HORIZON] - iv[i]
        if ana[i] == 0 or abs(actual) <= 0.001:
            continue
        rows.append({"month": d[:7], "hit": int((ana[i] > 0) == (actual > 0))})
    if not rows:
        return pd.DataFrame()
    m = pd.DataFrame(rows).groupby("month", as_index=False).agg(
        samples=("hit", "size"), hit_rate=("hit", "mean"))
    return m


def run():
    days, day_data = build_full_panel()
    df = assemble_panel(days, day_data)
    log(f"有效全量面板 {len(df)} 日，{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")
    return days, day_data, df


def report(df):
    yearly = walk_forward_yearly(df)
    focus = yearly_focus(df)
    overall = yearly[(yearly["samples"] > 0)]
    log("=== 年度命中率 ===")
    log(yearly.to_string(index=False))
    if len(focus):
        log("=== 极端年月度 ===")
        log(focus.to_string(index=False))

    # 图：全量 IV + 年度命中率柱状
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=df["date"], y=df["iv_near"], name="近月ATM IV",
                             line=dict(color="#185FA5", width=1)), secondary_y=False)
    fig.add_trace(go.Bar(x=yearly["year"],
                         y=[h * 100 if h == h else 0 for h in yearly["ana_hit"]],
                         name="类比命中率%", opacity=0.5), secondary_y=True)
    fig.add_trace(go.Scatter(x=yearly["year"],
                             y=[b * 100 if b == b else 0 for b in yearly["naive"]],
                             name="朴素基准%", line=dict(color="#C0392B", width=2)),
                  secondary_y=True)
    fig.update_layout(title="M8-④ 全量历史（2015 起）：IV 面板与年度方向命中率",
                      template="plotly_white", height=480)
    fig.update_yaxes(title_text="ATM IV", secondary_y=False)
    fig.update_yaxes(title_text="命中率 %", secondary_y=True)
    fig.write_html(REPORT_HTML)

    # Markdown 报告
    def _pct(v):
        return "—" if v != v else f"{v * 100:.1f}%"
    lines = [
        "# M8-④ 全量历史复现报告（2015-02 起）",
        "",
        f"- 面板范围：**{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}**，共 **{len(df)} 交易日**"
        f"（目标 {FULL_DAYS}，缓存缺口见年度样本列）",
        f"- 口径：与日常面板完全一致——WINDOW=120 / HORIZON=10 walk-forward，T+1 生效，"
        f"|ΔIV|≤0.001 跳过；命中率 = 类比引擎非零方向 vs {HORIZON} 日实际方向",
        f"- 数据源：上交所官方每日风险指标（`option_risk_indicator_sse`，逐日缓存可续拉）"
        f"+ 新浪/东财 ETF 日线",
        "",
        "## 年度方向命中率",
        "",
        "| 年份 | 样本 | 类比引擎 | 分位数引擎 | 朴素基准 | 高IV期命中 | 低IV期命中 |",
        "|---|---|---|---|---|---|---|",
    ]
    for _, r in yearly.iterrows():
        lines.append(
            f"| {r['year']} | {r['samples']} | {_pct(r['ana_hit'])} | "
            f"{_pct(r['pct_hit'])} | {_pct(r['naive'])} | {_pct(r['hi_hit'])} | {_pct(r['lo_hit'])} |")
    if len(focus):
        lines += ["", "## 极端行情月度明细（2020 疫情 / 2024 924 行情）", "",
                  "| 月份 | 样本 | 命中率 |", "|---|---|---|"]
        for _, r in focus.iterrows():
            lines.append(f"| {r['month']} | {r['samples']} | {r['hit_rate'] * 100:.1f}% |")
    lines += [
        "",
        "## 结论要点",
        "",
        "- 图表：`docs/report-m8-4-chart.html`（IV 全量曲线 + 年度命中率 vs 基准）",
        "- 生成时间：" + pd.Timestamp.now().strftime("%Y-%m-%d %H:%M"),
        "",
        "> 诚实边界：缓存缺口年份（样本列偏少者）结论弱化；2020 年初上交所风险指标"
        "偶发缺失（探测确认，重试可补）；本报告为统计复核，非收益承诺。",
    ]
    os.makedirs(os.path.dirname(REPORT_MD), exist_ok=True)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"报告已写：{REPORT_MD}")
    log(f"图表已写：{REPORT_HTML}")
    return yearly, focus


if __name__ == "__main__":
    if "--fetch" in sys.argv:
        build_full_panel()
    else:
        _, _, df = run()
        report(df)
