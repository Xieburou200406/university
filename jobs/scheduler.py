# -*- coding: utf-8 -*-
"""jobs/scheduler.py — 每日收盘后的正式管线调度器（§500 Windows 本机形态）。

schtasks 黑名单内无法代注册，请以当前用户身份手工注册一次（管理员不需要）：
  schtasks /Create /F /TN "VolDemoDaily" /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 15:30 ^
    /TR "\"C:/Users/abigail/.workbuddy/binaries/python/envs/default/Scripts/python.exe\" ^
         \"C:/Users/abigail/WorkBuddy/2026-09-30-14-19-09/vol-demo/jobs/scheduler.py\""
（PowerShell 下省略行尾 ^ 并写成单行。）

流程（每交易日 15:30，非交易日自动空转）：
  1. 刷新官方交易日历缓存（失败降级，不阻断）
  2. trigger_pipeline：面板 → daily_metric + signal + 持仓收盘估值快照（防重入锁内）
  3. run_diagnosis：六步排查留痕（只读）
  4. opslog EOD 备份
幂等：同日重跑 = daily_metric/signal/快照按当日覆盖，无重复落库。只生成建议，绝不联网下单。
"""
import os
import sys
from datetime import date

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "backend"))
os.environ.setdefault("VOL_DB", os.path.join(_ROOT, "data", "vol.db"))


def main() -> int:
    from db.session import make_session
    from vol.calendar import get_calendar, refresh_calendar_cache
    from vol.opslog import eod_backup, log_event, read_events

    print(f"[{date.today()}] scheduler start", flush=True)

    # ① 非交易日空转（周一兜周五：15:30 触发即可，节假日交给日历）
    cal = get_calendar()
    if not cal.is_trading_day(date.today()):
        log_event("D", "scheduler", {"skip": "non_trading_day"})
        print("非交易日，空转退出", flush=True)
        return 0

    # ② 交易日历缓存刷新（显式联网窗口；失败仅降级）
    ok = refresh_calendar_cache()
    log_event("D", "calendar", {"refresh": "ok" if ok else "degraded"})

    # ③ 正式管线（面板 → 指标/信号/建议素材 + 持仓快照；重入由内部锁拒绝）
    from api.services import run_diagnosis, trigger_pipeline
    SM, _ = make_session()
    s = SM()
    try:
        run = trigger_pipeline(s)
        summary = run.error or ""
        print(f"pipeline: {run.status} {summary}", flush=True)

        # 周五顺带命中率周度复核（M6-③；同周覆盖幂等，低样本 fail-soft）
        if date.today().weekday() == 4:
            from api.services import run_weekly_review
            rv = run_weekly_review(s)
            print(f"weekly review: {rv}", flush=True)

        # ④ 六步排查留痕（只读；结果只进日志，不出阻断）
        diag = run_diagnosis(s)
        bad = [k for k, v in diag.items() if isinstance(v, dict) and v.get("ok") is False]
        if bad:
            log_event("X", "diagnosis", {"failed_steps": ",".join(bad)})
    except PermissionError as e:
        print(f"pipeline 重入拒绝：{e}", flush=True)      # 上一次还在跑，跳过本次
        return 0
    except Exception as e:                                # noqa: BLE001 —— 调度器永不裸崩
        log_event("X", "scheduler", {"error": str(e)[:150]})
        print(f"pipeline 失败：{e}", flush=True)
        return 1
    finally:
        s.close()

    # ⑤ EOD 日志备份
    p = eod_backup(os.environ["VOL_DB"])
    print(f"eod backup: {p}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
