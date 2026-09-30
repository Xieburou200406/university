# -*- coding: utf-8 -*-
"""
weekly_review.py — 类比引擎命中率周度复核（M4 收尾项）
做什么：用本地缓存重算滚动样本外方向命中率 vs 朴素基准，追加写入复核日志，
并与上次结果对比：命中率显著恶化（跌破朴素基准）时退出码 1 并打印告警。
调度建议（schtasks 每周五 16:00）：
  schtasks /Create /TN "VolDemoWeeklyReview" /SC WEEKLY /D FRI /ST 16:00 ^
    /TR "\"C:/Users/abigail/.workbuddy/binaries/python/envs/default/Scripts/python.exe\" \"C:/Users/abigail/WorkBuddy/2026-09-30-14-19-09/vol-demo/weekly_review.py\""
只读本地缓存，不联网。
"""
import json
import os
import sqlite3
from datetime import datetime

import numpy as np
import pandas as pd

from m2_run import CACHE_DIR, WINDOW, HORIZON, fetch_dates, expiry_from_code
from m3_run import build_hist_df, analog_states
from run_demo import log, UNDERLYING, UNIT, R_FREE

REVIEW_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "review_log.csv")
ALERT_DROP = 0.0   # 命中率低于朴素基准 + ALERT_DROP 即告警


def main():
    days = fetch_dates(520)
    panel = {}
    for d, px in days:
        fp = os.path.join(CACHE_DIR, f"ri_v2_{d.replace('-', '')}.csv")
        if not os.path.exists(fp):
            continue
        t = pd.read_csv(fp, dtype={"code": str})
        t["exp_dt"] = t["code"].map(expiry_from_code)
        panel[d] = t
    df = build_hist_df(panel, days)
    log(f"面板 {len(df)} 日（{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}）")

    ana = analog_states(df)
    hits, actuals = [], []
    for i in range(WINDOW, len(df) - HORIZON):
        if ana[i] == 0:
            continue
        actual = df["iv_near"][i + HORIZON] - df["iv_near"][i]
        if abs(actual) <= 0.001:
            continue
        hits.append((ana[i] > 0) == (actual > 0))
        actuals.append(actual > 0)
    n = len(hits)
    if n < 30:
        log(f"有效样本仅 {n}，不足 30，跳过本次记录")
        return
    hit_rate = float(np.mean(hits))
    baseline = float(max(np.mean(actuals), 1 - np.mean(actuals)))
    edge = hit_rate - baseline
    verdict = "OK" if edge >= ALERT_DROP else "ALERT:引擎增量消失，建议降级为参考"
    log(f"命中率 {hit_rate:.2%} vs 朴素基准 {baseline:.2%}（增量 {edge:+.2%}，样本 {n}）→ {verdict}")

    # 与上次对比
    prev_edge = None
    if os.path.exists(REVIEW_LOG):
        hist = pd.read_csv(REVIEW_LOG)
        if len(hist):
            prev_edge = float(hist.iloc[-1]["edge"])
            if prev_edge is not None and edge < prev_edge - 0.05:
                log(f"⚠️ 增量较上次下滑 {prev_edge - edge:+.2%}，加密复核频率")
    # 分 regime 粗分：高 IV 期（>中位）vs 低 IV 期
    med = float(df["iv_near"].median())
    hi_hits, lo_hits = [], []
    for i in range(WINDOW, len(df) - HORIZON):
        if ana[i] == 0:
            continue
        actual = df["iv_near"][i + HORIZON] - df["iv_near"][i]
        if abs(actual) <= 0.001:
            continue
        h = (ana[i] > 0) == (actual > 0)
        (hi_hits if df["iv_near"][i] > med else lo_hits).append(h)
    row = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "panel_days": len(df), "samples": n,
        "hit_rate": round(hit_rate, 4), "baseline": round(baseline, 4),
        "edge": round(edge, 4),
        "hit_rate_high_iv": round(float(np.mean(hi_hits)), 4) if hi_hits else np.nan,
        "hit_rate_low_iv": round(float(np.mean(lo_hits)), 4) if lo_hits else np.nan,
        "verdict": verdict,
    }
    os.makedirs(os.path.dirname(REVIEW_LOG), exist_ok=True)
    pd.DataFrame([row]).to_csv(REVIEW_LOG, mode="a",
                               header=not os.path.exists(REVIEW_LOG), index=False)
    log(f"已追加复核日志 {REVIEW_LOG}")
    print(json.dumps(row, ensure_ascii=False))

    # 告警写入 signal_log，面板可见
    if verdict.startswith("ALERT"):
        con = sqlite3.connect(os.path.join(os.path.dirname(REVIEW_LOG), "..", "vol_demo.db"))
        con.execute("""CREATE TABLE IF NOT EXISTS signal_log(
            date TEXT, signal TEXT, reason TEXT, status TEXT, PRIMARY KEY(date, signal))""")
        con.execute("INSERT OR REPLACE INTO signal_log VALUES (?,?,?,?)",
                    (datetime.now().strftime("%Y-%m-%d"), "ANALOG_REVIEW_ALERT",
                     f"edge={edge:+.4f}", "REVIEW"))
        con.commit()
        con.close()


if __name__ == "__main__":
    main()
