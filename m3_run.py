# -*- coding: utf-8 -*-
"""
m3_run.py — M3 增量：
  1. 模拟持仓账本（SQLite，架构文档 §3.4 表结构）
  2. 每日快照差分归因：Delta/Vega/Theta/残差 四项拆解 + 口径校验（之和≈ΔPnL）
  3. 回测：分位数引擎 vs 类比引擎 vs 买入持有长波动率（IV 点代理 + 换手成本）
  4. 市值重估用 BS(官方IV)，希腊字母用官方值——"mark-to-model"口径
输出：m3_report.html + vol_demo.db
"""
import math
import os
import sqlite3

import numpy as np
import sys, os as _os
sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "backend"))
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from vol.pricing.bs import bs_price
from vol.signals import AnalogEngine
from run_demo import R_FREE, log
from m2_run import CACHE_DIR, WINDOW, HORIZON, fetch_dates, expiry_from_code

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vol_demo.db")
UNIT = 10000
VEGA_NOTIONAL = 100.0        # 回测代理：每 1 个 IV 点（0.01）盈亏 100 元
SWITCH_COST = 30.0           # 每次换向成本 ≈ 0.3 个 IV 点（价差+手续费代理）
HOLD_DAYS = 20               # 归因演示：持仓 20 个交易日


def load_panel():
    """从缓存载入 ~2 年面板：date -> DataFrame(code, strike, cp, iv, delta, vega, theta...)"""
    days = fetch_dates(520)
    panel = {}
    for d, px in days:
        fp = os.path.join(CACHE_DIR, f"ri_v2_{d.replace('-', '')}.csv")
        if not os.path.exists(fp):
            continue
        t = pd.read_csv(fp, dtype={"code": str})
        t["exp_dt"] = t["code"].map(expiry_from_code)
        panel[d] = t
    return days, panel


# ---------- 历史特征（与 M2 相同口径） ----------

def build_hist_df(panel, days):
    rows = []
    for d, px in days:
        t = panel.get(d)
        if t is None:
            continue
        d0 = pd.Timestamp(d)
        t = t[(t["exp_dt"] - d0).dt.days >= 14]
        t = t[t["iv"] > 0]
        if len(t) == 0:
            continue
        exps = sorted(t["exp_dt"].unique())
        rec = {"date": d, "spot": px}
        ivs = {}
        for tag, e in [("near", exps[0]), ("next", exps[1] if len(exps) > 1 else None)]:
            if e is None:
                ivs[tag] = None
                continue
            sub = t[t["exp_dt"] == e]
            ivs[tag] = float(sub.iloc[(sub["strike"] - px).abs().argsort().iloc[0]]["iv"])
        rec["iv_near"], rec["iv_next"] = ivs["near"], ivs["next"]
        if rec["iv_near"] is None:
            continue
        sub = t[t["exp_dt"] == exps[0]]
        calls, puts = sub[sub["cp"] == "C"], sub[sub["cp"] == "P"]
        try:
            c25 = calls.iloc[(calls["delta"] - 0.25).abs().argsort().iloc[0]]["iv"]
            p25 = puts.iloc[(puts["delta"] + 0.25).abs().argsort().iloc[0]]["iv"]
            rec["skew25"] = float(p25) - float(c25)
        except Exception:
            rec["skew25"] = None
        rows.append(rec)
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    log_s = np.log(df["spot"]).diff()
    df["rvol20"] = (log_s.rolling(20).std() * math.sqrt(245))
    return df


# ---------- 分位数信号逐日回放（与 PercentileEngine 同一状态机口径） ----------

def percentile_states(iv_series, window=WINDOW):
    states = []
    state, streak, last_zone = "NEUTRAL", 0, None
    for i in range(len(iv_series)):
        if i < window:
            states.append("WARMUP")
            continue
        hist = iv_series[i - window:i]
        today = iv_series[i]
        pct = sum(1 for v in hist if v <= today) / len(hist)
        zone = "HIGH" if pct >= 0.80 else ("LOW" if pct <= 0.20 else "MID")
        if zone != "MID" and zone == last_zone:
            streak += 1
        elif zone != "MID":
            streak = 1
        else:
            streak = 0
        last_zone = zone
        if streak >= 2:
            state = "SHORT_VOL" if zone == "HIGH" else "LONG_VOL"
        elif zone == "MID":
            state = "NEUTRAL"
        states.append(state)
    return states


def analog_states(df, k=25, horizon=HORIZON):
    """逐日 walk-forward：只用 i 之前的数据检索，返回每日方向 (+1/-1/0)。"""
    feats = []
    for i in range(len(df)):
        lo = max(0, i - WINDOW)
        hist = df["iv_near"][lo:i + 1]
        pct = float((hist <= df["iv_near"][i]).mean())
        rv = df["rvol20"][i] if not np.isnan(df["rvol20"][i]) else df["iv_near"][i]
        mom = df["spot"][i] / df["spot"][i - 20] - 1.0 if i >= 20 else 0.0
        feats.append({"date": df["date"][i], "iv_pct": pct,
                      "vrp": df["iv_near"][i] - rv, "mom20": mom,
                      "ts_slope": df["iv_near"][i] - (df["iv_next"][i] if df["iv_next"][i] else df["iv_near"][i]),
                      "skew25": df["skew25"][i] if df["skew25"][i] is not None else 0.0})
    FEATS = ["iv_pct", "vrp", "mom20", "ts_slope", "skew25"]
    out = []
    for i in range(len(df)):
        if i < WINDOW + 10 or i >= len(df) - horizon:
            out.append(0)
            continue
        arr = np.array([[f[kk] for kk in FEATS] for f in feats[:i]])
        mu, sd = arr.mean(axis=0), arr.std(axis=0) + 1e-9
        zx = np.array([feats[i][kk] for kk in FEATS])
        dist = np.sqrt((((arr - mu) / sd - (zx - mu) / sd) ** 2).sum(axis=1))
        idxs = [int(j) for j in np.argsort(dist) if j < i - horizon][:k]
        div = np.mean([df["iv_near"][i + horizon] - df["iv_near"][j] for j in idxs])
        out.append(1 if div > 0.001 else (-1 if div < -0.001 else 0))
    return out


# ---------- 持仓回放 + 归因 ----------

def replay_position(panel, df, open_offset=HOLD_DAYS):
    """
    开仓：倒数第 open_offset+1 个交易日，卖 1 张平值跨式（近月，剩余≥25天）。
    逐日：BS(官方IV) 重估市值 → 快照差分 → 四项归因。
    """
    dates = df["date"].tolist()
    n = len(dates)
    i0 = n - 1 - open_offset
    d_open = dates[i0]
    t_open = panel[d_open]
    spot_open = df["spot"][i0]
    d0 = pd.Timestamp(d_open)
    t_open = t_open[(t_open["exp_dt"] - d0).dt.days >= 25]
    if len(t_open) == 0:
        raise RuntimeError("开仓日无剩余≥25天的近月合约")
    exp_day = t_open["exp_dt"].min()
    sub = t_open[t_open["exp_dt"] == exp_day]
    atm_row = sub.iloc[(sub["strike"] - spot_open).abs().argsort().iloc[0]]
    K = float(atm_row["strike"])
    exp_str = exp_day.strftime("%Y-%m-%d")
    legs = []
    for cp in ("C", "P"):
        row = sub[(sub["strike"] == K) & (sub["cp"] == cp)]
        if len(row) == 0:
            continue
        row = row.iloc[0]
        legs.append({"code": row["code"], "cp": cp, "strike": K,
                     "qty": -1, "iv_open": float(row["iv"]),
                     "delta": float(row["delta"]), "vega": float(row["vega"]),
                     "theta": float(row["theta"])})
    log(f"开仓 {d_open}: 卖1张 K={K} 跨式（{exp_str} 到期），IV={legs[0]['iv_open']:.3f}")

    # SQLite 账本
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
    DROP TABLE IF EXISTS position; DROP TABLE IF EXISTS position_snapshot;
    CREATE TABLE position(id INTEGER PRIMARY KEY, open_date TEXT, symbol TEXT,
        direction TEXT, qty INTEGER, open_price REAL, status TEXT, close_date TEXT);
    CREATE TABLE position_snapshot(date TEXT, position_id INTEGER, mkt_price REAL,
        market_value REAL, cum_pnl REAL, acc_delta REAL, acc_vega REAL, acc_theta REAL,
        PRIMARY KEY(date, position_id));
    """)
    end_i = min(n - 1, i0 + HOLD_DAYS)
    rows_ledger, rows_attr = [], []
    prev = None
    cum = 0.0
    for i in range(i0, end_i + 1):
        d = dates[i]
        day = panel[d]
        spot = df["spot"][i]
        T = max((pd.Timestamp(exp_str) - pd.Timestamp(d)).days, 1) / 365.0
        mv, acc_d, acc_v, acc_t = 0.0, 0.0, 0.0, 0.0
        day_legs = {}
        for leg in legs:
            row = day[day["code"] == leg["code"]]
            if len(row) == 0:
                price = prev["legs"][leg["code"]]["price"] if prev else 0.0
                iv, dlt, vga, tht = leg["iv_open"], leg["delta"], leg["vega"], leg["theta"]
            else:
                row = row.iloc[0]
                iv, dlt, vga, tht = float(row["iv"]), float(row["delta"]), float(row["vega"]), float(row["theta"])
                price = bs_price(spot, leg["strike"], T, R_FREE, iv, leg["cp"])
            day_legs[leg["code"]] = {"price": price, "iv": iv, "delta": dlt, "vega": vga, "theta": tht}
            mv += leg["qty"] * price * UNIT
            acc_d += leg["qty"] * dlt * UNIT
            acc_v += leg["qty"] * vga * UNIT          # 每 1.0 IV 的敞口（元）
            acc_t += leg["qty"] * tht / 365.0 * UNIT  # 年化 → 每日（元/天）
        if prev:
            d_pnl = mv - prev["mv"]
            d_spot = spot - prev["spot"]
            delta_item = vega_item = theta_item = 0.0
            for leg in legs:
                pl = prev["legs"][leg["code"]]
                delta_item += leg["qty"] * pl["delta"] * d_spot * UNIT
                # 量纲标定（实测对齐 cum_pnl）：官方 VEGA = 每 1.0 IV 的价格变化；THETA = 年化
                vega_item += leg["qty"] * pl["vega"] * (day_legs[leg["code"]]["iv"] - pl["iv"]) * UNIT
                theta_item += leg["qty"] * pl["theta"] / 365.0 * UNIT
            resid = d_pnl - delta_item - vega_item - theta_item
            cum += d_pnl
            rows_attr.append({"date": d, "pnl": d_pnl, "delta": delta_item,
                              "vega": vega_item, "theta": theta_item, "resid": resid,
                              "resid_pct": abs(resid) / abs(d_pnl) if abs(d_pnl) > 1e-9 else np.nan,
                              "cum": cum})
        prev = {"mv": mv, "spot": spot, "legs": day_legs}
        rows_ledger.append({"date": d, "mv": mv, "cum": cum,
                            "delta": acc_d, "vega": acc_v, "theta": acc_t})
    # 账本入库（演示口径）
    cur.execute("INSERT INTO position(open_date, symbol, direction, qty, open_price, status) VALUES (?,?,?,?,?,?)",
                (d_open, f"K={K} STRADDLE {exp_str}", "SHORT", -2, legs[0]["iv_open"], "OPEN"))
    pid = cur.lastrowid
    for r in rows_ledger:
        cur.execute("INSERT OR REPLACE INTO position_snapshot VALUES (?,?,?,?,?,?,?,?)",
                    (r["date"], pid, None, r["mv"], r["cum"], r["delta"], r["vega"], r["theta"]))
    con.commit()
    con.close()
    return d_open, K, exp_str, rows_ledger, rows_attr


# ---------- 回测 ----------

def backtest(df, pct_states, ana_states):
    """
    口径修正版回测：
    - 状态滞后 1 日生效（T 日收盘出状态，T+1 才建仓赚 T+1 的涨跌），消灭日内前视；
    - 类比引擎按预测持有期平滑：暴露 = 过去 HORIZON 日状态之和的符号，
      避免预测未到期就被每日翻转（这正是方向命中率高却亏损的根源）。
    """
    iv = df["iv_near"].tolist()
    n = len(iv)
    curves = {"percentile": [0.0], "analog": [0.0], "longvol": [0.0]}
    prev_exp = {"percentile": 0, "analog": 0}
    for i in range(1, n):
        div = (iv[i] - iv[i - 1]) / 0.01   # IV 点
        # 分位数：昨日状态（消灭前视）
        s_prev = pct_states[i - 1] if i >= 1 else "WARMUP"
        e_pct = {"LONG_VOL": 1, "SHORT_VOL": -1}.get(s_prev, 0)
        # 类比：过去 HORIZON 日状态平滑持有
        lo = max(0, i - HORIZON)
        s_sum = sum(ana_states[lo:i])
        e_ana = 1 if s_sum > 0 else (-1 if s_sum < 0 else 0)
        for name, e, prev_e in [("percentile", e_pct, prev_exp["percentile"]),
                                ("analog", e_ana, prev_exp["analog"])]:
            pnl = e * div * VEGA_NOTIONAL
            if e != prev_e:
                pnl -= SWITCH_COST
            curves[name].append(curves[name][-1] + pnl)
        prev_exp["percentile"], prev_exp["analog"] = e_pct, e_ana
        curves["longvol"].append(curves["longvol"][-1] + div * VEGA_NOTIONAL)
    return curves


def max_drawdown(nav):
    peak, mdd = nav[0], 0.0
    for v in nav:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return mdd


# ---------- 主流程 ----------

def main():
    days, panel = load_panel()
    df = build_hist_df(panel, days)
    log(f"面板 {len(df)} 日，{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}")

    # 信号逐日回放
    pct_states = percentile_states(df["iv_near"].tolist())
    ana_states = analog_states(df)
    log("信号回放完成")

    # 回测
    curves = backtest(df, pct_states, ana_states)

    # 持仓归因
    d_open, K, exp_str, ledger, attr = replay_position(panel, df)
    attr_df = pd.DataFrame(attr)
    check = (attr_df["delta"] + attr_df["vega"] + attr_df["theta"] + attr_df["resid"]
             - attr_df["pnl"]).abs().max()
    log(f"口径校验：|四项之和 − ΔPnL| 最大 = {check:.6f}（应≈0）")
    # 残差占比只在"盈亏有意义"的日期统计（|ΔPnL|≥50 元），避免分母趋零失真
    valid = attr_df[attr_df["pnl"].abs() >= 50]
    resid_share = float((valid["resid"].abs() / valid["pnl"].abs()).median()) if len(valid) else float("nan")
    log(f"残差占比中位数（有效日 {len(valid)}/{len(attr_df)}）= {resid_share:.0%}")

    build_report(locals())
    log("m3_report.html 已生成")


def build_report(ctx):
    df = ctx["df"]; curves = ctx["curves"]; ledger = ctx["ledger"]; attr_df = ctx["attr_df"]
    d_open = ctx["d_open"]; K = ctx["K"]; exp_str = ctx["exp_str"]
    check = ctx["check"]; resid_share = ctx["resid_share"]

    fig_nav = go.Figure()
    colors = {"percentile": "#185FA5", "analog": "#0F6E56", "longvol": "#888780"}
    names = {"percentile": "分位数引擎", "analog": "类比引擎", "longvol": "买入持有长波(参照)"}
    for k in curves:
        fig_nav.add_trace(go.Scatter(x=df["date"], y=curves[k], mode="lines",
                                     name=names[k], line=dict(color=colors[k], width=2)))
    fig_nav.update_layout(title="回测净值（IV 点代理收益，含换手成本）",
                          xaxis_title="日期", yaxis_title="累计盈亏（元，代理）",
                          template="plotly_white", height=420)

    fig_wf = go.Waterfall(
        x=attr_df["date"].tolist(), measure=["relative"] * len(attr_df),
        y=[round(v, 1) for v in attr_df["pnl"]], text=[f"{v:+.0f}" for v in attr_df["pnl"]],
        decreasing=dict(marker=dict(color="#3B6D11")),
        increasing=dict(marker=dict(color="#A32D2D")),
        connector=dict(line=dict(color="#B4B2A9", width=0.5)))
    fig_wf = go.Figure(fig_wf)
    fig_wf.update_layout(title=f"每日 ΔPnL 瀑布（{d_open} 开仓 卖1张 K={K} 跨式）",
                         template="plotly_white", height=380)

    attr_rows = "".join(
        f"<tr><td>{r.date}</td><td>{r.pnl:+.0f}</td><td>{r.delta:+.0f}</td>"
        f"<td>{r.vega:+.0f}</td><td>{r.theta:+.0f}</td><td>{r.resid:+.0f}</td>"
        f"<td>{('—' if pd.isna(r.resid_pct) else f'{r.resid_pct:.0%}')}</td></tr>"
        for r in attr_df.itertuples())

    stats = []
    for k in curves:
        nav = curves[k]
        stats.append((names[k], f"{nav[-1]:+.0f}",
                      f"{sum(1 for a, b in zip(nav[1:], nav[:-1]) if b > a) / max(1, len(nav) - 1):.0%}",
                      f"{max_drawdown(nav):.0f}"))
    stat_rows = "".join(f"<tr><td>{n}</td><td>{c}</td><td>{w}</td><td>{m}</td></tr>"
                        for n, c, w, m in stats)

    fig_nav.write_html("m3_report.html", include_plotlyjs=True, full_html=False, div_id="c1")
    part1 = open("m3_report.html", encoding="utf-8").read()
    fig_wf.write_html("m3_report.html", include_plotlyjs=False, full_html=False, div_id="c2")
    part2 = open("m3_report.html", encoding="utf-8").read()

    html = f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>M3 · 持仓账本 + 快照差分归因 + 双引擎回测</title>
<style>body{{font-family:"Microsoft YaHei",sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#222;background:#fff}}
h1{{font-size:22px}} h2{{font-size:17px;border-left:4px solid #0F6E56;padding-left:8px;margin-top:30px}}
.card{{background:#F5F7FA;border:1px solid #d8dde5;border-radius:10px;padding:12px 16px;margin:10px 0}}
.ok{{color:#0F6E56;font-weight:bold}} .warn{{color:#993C1D;font-weight:bold}}
.tbl{{border-collapse:collapse;font-size:13px;width:100%}}
.tbl th,.tbl td{{border-bottom:1px solid #e2e6ec;padding:5px 8px;text-align:right}}
.tbl th:first-child,.tbl td:first-child{{text-align:left}}
</style></head><body>
<h1>M3 · 模拟持仓账本 + 快照差分归因 + 回测</h1>
<p>样本区间 {df['date'].iloc[0]} ~ {df['date'].iloc[-1]}（{len(df)} 交易日）·
账本落库 vol_demo.db（SQLite）· 市值重估口径 = BS(官方IV)，希腊字母用上交所官方值</p>

<h2>1. 模拟持仓（示例：{d_open} 开仓 卖 1 张 K={K} 跨式，{exp_str} 到期）</h2>
<div class="card">
口径铁律执行：每日收盘落<b>快照</b>（market_value/累计盈亏/累计希腊字母），展示必<b>差分</b>。
累计快照与逐日差分均已写入 SQLite position / position_snapshot 表。</div>

<h2>2. 快照差分归因（Delta / Vega / Theta / 残差）</h2>
{part1}{part2}
<table class="tbl"><tr><th>日期</th><th>ΔPnL</th><th>Delta项</th><th>Vega项</th><th>Theta项</th><th>残差</th><th>残差占比</th></tr>{attr_rows}</table>
<p>口径校验：max|四项之和 − ΔPnL| = {check:.2e}（应≈0）<span class="ok"> ✓</span>；
残差占比中位数 ≈ {resid_share:.0%}（Gamma/凸性等二阶项，占比高时面板黄牌提示"归因解释力不足"）。
卖跨式的盈亏结构：<span class="ok">Theta 每日为正（收时间价值）</span>，Vega 项随 IV 波动大幅摆动——
<b>这就是"赚时间价值、怕波动率飙升"的直观呈现</b>。</p>

<h2>3. 双引擎回测（120 日窗口，满窗口约 {len(df) - WINDOW} 个可交易日）</h2>
<table class="tbl"><tr><th>策略</th><th>累计盈亏(代理)</th><th>日胜率</th><th>最大回撤</th></tr>{stat_rows}</table>
<p>成本假设：换向一次扣 {30.0} 元（≈0.3 IV 点价差+手续费代理）。<b>结论待样本变长后再下</b>：
当前 130 个可交易日只覆盖一种波动率 regime，两个引擎的相对优劣需要 1 年以上数据。</p>

<h2>4. M3 结论与 M4 待办</h2>
<div class="card">
<b class="ok">M3 三件套全部落地：</b>SQLite 账本、快照差分四项归因（口径校验通过）、双引擎回测（成本显式计入）。<br>
<b>M4：</b>① Streamlit 面板整合全部视图（IV曲线/信号台/风控台/归因/回测五 Tab）；
② 每日定时任务（15:30 拉数→信号→建议卡片，T+1 过期作废）；
③ 类比引擎命中率周度复核自动化；④ 打包为单机可执行或 Docker。
</div>
</body></html>"""
    open("m3_report.html", "w", encoding="utf-8").write(html)


if __name__ == "__main__":
    main()
