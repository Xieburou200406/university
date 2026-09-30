# -*- coding: utf-8 -*-
"""
probe_akshare.py — AKShare 接口探针（架构文档 M1 第一步）
逐个尝试候选期权接口，实测哪些能用、字段长什么样。
铁律：禁止凭记忆猜字段，一切以实测输出为准。
"""
import importlib
import json
import sys
import traceback

import akshare as ak

CANDIDATES = [
    # (函数名, 调用参数)
    ("fund_etf_spot_em", {"symbol": "上证50ETF"}),
    ("option_current_em", {"symbol": "50ETF期权"}),
    ("option_sse_codes_sina", {"symbol": "50ETF", "contract": "202610"}),
    ("option_sse_spot_sina", {"symbol": "10005674"}),
    ("option_sse_daily_sina", {"symbol": "10005674"}),
    ("option_sze_daily_sina", {"symbol": "90000882"}),
]


def probe():
    results = []
    for name, kwargs in CANDIDATES:
        fn = getattr(ak, name, None)
        entry = {"func": name, "kwargs": kwargs, "exists": fn is not None}
        if fn is None:
            results.append(entry)
            continue
        try:
            df = fn(**kwargs)
            entry["ok"] = True
            entry["rows"] = len(df)
            entry["columns"] = list(df.columns)
            entry["head"] = df.head(3).to_dict(orient="records")
        except Exception as e:  # noqa: BLE001
            entry["ok"] = False
            entry["error"] = f"{type(e).__name__}: {e}"
            entry["trace"] = traceback.format_exc(limit=2)
        results.append(entry)

    print("=" * 70)
    print("AKSHARE 接口探针结果")
    print("=" * 70)
    for r in results:
        status = "MISSING" if not r.get("exists") else ("OK" if r.get("ok") else "FAIL")
        print(f"\n[{status}] {r['func']}  kwargs={r['kwargs']}")
        if status == "MISSING":
            print("  当前 akshare 版本无此函数")
        elif status == "OK":
            print(f"  rows={r['rows']}  columns={r['columns']}")
            print("  sample:", json.dumps(r["head"], ensure_ascii=False, default=str)[:600])
        else:
            print("  error:", r["error"])

    usable = [r["func"] for r in results if r.get("ok")]
    print("\n" + "=" * 70)
    print(f"可用接口: {usable if usable else '无 —— 需启用模拟数据兜底'}")
    return usable


if __name__ == "__main__":
    probe()
    sys.exit(0)
