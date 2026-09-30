# -*- coding: utf-8 -*-
"""
daily_job.py — 每日定时任务（M4）
建议由 Windows 任务计划在每个交易日 15:30 调用：
  schtasks /Create /TN "VolDemoDaily" /SC WEEKLY /D MON,TUE,WED,THU,FRI ^
    /ST 15:30 /TR "\"C:/Users/abigail/.workbuddy/binaries/python/envs/default/Scripts/python.exe\" \"C:/Users/abigail/WorkBuddy/2026-09-30-14-19-09/vol-demo/daily_job.py\""
流程：拉官方指标(显式联网) → 更新缓存 → 信号 → 风控口径检查 → 建议+流水落库(signal_log)
只生成建议，绝不联网下单。
"""
import os
import sqlite3
from datetime import datetime, timedelta

from m2_run import build_daily_history, WINDOW
from run_demo import log
from signals import PercentileEngine
import pandas as pd
import numpy as np

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vol_demo.db")


def next_trading_day_like_exec_window(today):
    """T+1 执行窗口提示（简单取下一自然日，正式版用交易日历）。"""
    return (today + timedelta(days=1)).strftime("%Y-%m-%d") + " 9:35~10:00"


def main():
    log("===== 每日任务开始 =====")
    df = build_daily_history()
    log(f"历史面板 {len(df)} 日（{df['date'].iloc[0]} ~ {df['date'].iloc[-1]}）")

    eng = PercentileEngine(window=WINDOW)
    sig = eng.run(df["iv_near"].tolist())
    iv_now = df["iv_near"].iloc[-1]
    d = df["date"].iloc[-1]

    # 双窗口对照（防窗口选择自欺）
    pct40 = float((df["iv_near"].tail(40) <= iv_now).mean())

    # 类比引擎方向（复用 m3 的 walk-forward 逻辑取最后一天）
    from m3_run import analog_states
    ana = analog_states(df)[-1]

    # 建议卡片
    action = {"SHORT_VOL": "倾向卖波（收权利金方向）", "LONG_VOL": "倾向买波（波动率多头方向）",
              "NEUTRAL": "观望"}.get(sig["signal"], "观望")
    if sig.get("confirm_needed", 0) > 0:
        action += f"（状态翻转待确认，还需 {sig['confirm_needed']} 日）"
    if ana != 0:
        action += f"；类比引擎{'同向增强' if (ana == 1 and sig['signal'] == 'LONG_VOL') or (ana == -1 and sig['signal'] == 'SHORT_VOL') else '方向不一致，降级参考'}"

    card = f"""
┌─────────────────────────────────────────────┐
│ 建议卡片  {d}（基于 {d} 收盘截面）            │
│ 信号：{sig['signal']:<12} → {action}
│ ATM IV：{iv_now * 100:.2f}%  120日分位：{sig.get('iv_pct')}  40日分位：{pct40:.2f}
│ 有效期：{next_trading_day_like_exec_window(datetime.strptime(d, '%Y-%m-%d'))}
│ 执行前必须用 T+1 最新行情复核风控条件链       │
│ 本系统只生成建议，不自动下单                 │
└─────────────────────────────────────────────┘"""
    print(card)

    # 流水落库
    con = sqlite3.connect(DB_PATH)
    con.execute("""CREATE TABLE IF NOT EXISTS signal_log(
        date TEXT, signal TEXT, reason TEXT, status TEXT, PRIMARY KEY(date, signal))""")
    con.execute("INSERT OR REPLACE INTO signal_log VALUES (?,?,?,?)",
                (d, sig["signal"], f"iv={iv_now:.4f}, pct120={sig.get('iv_pct')}, pct40={pct40:.2f}, analog={ana}",
                 "PENDING_EXEC_T1"))
    con.commit()
    con.close()
    log("信号流水已写入 signal_log（T+1 未执行则明日作废）")
    log("===== 每日任务结束 =====")


if __name__ == "__main__":
    main()
