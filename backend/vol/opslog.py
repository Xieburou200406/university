# -*- coding: utf-8 -*-
"""结构化事件日志（M7-①，§17.1/§17.3）：单行制 + 两级粒度 + daily/curr 轮转。

格式（grep/awk 数字段法直接可用）：
    HH:MM:SS|code|key|k=v k=v
    code ∈ D(数据) F(特征) S(信号) A(建议) B(预算) P(管线) X(异常) —— 明细级，排查专用
    U(日报级聚合计数) —— 只回答"今天健康吗"，禁止用于排查（对齐 QS `U|` 禁令）

负记录原则：X| 一律落明文原因——无信号的那天，日志必须能回答"为什么没发生"。

轮转（daily/curr 同构，Windows 无软链 → 标记文件 + 改名定格）：
    logs/curr.log          ← 活跃文件，所有组件只写它
    logs/curr.log.date     ← 创建日标记
    logs/ops-YYYYMMDD.log  ← 换日首写时 curr.log 改名定格 = 回滚底牌

环境变量 OPSLOG_DIR 可重定向日志根目录（测试隔离用）。
"""
import os
from datetime import date, datetime
from pathlib import Path

CODES = {"D", "F", "S", "A", "B", "P", "X", "U"}
_CURR = "curr.log"
_MARKER = "curr.log.date"
_ARCHIVE = "ops-{}.log"


def _log_dir(log_dir) -> Path:
    if log_dir:
        p = Path(log_dir)
    else:
        env = os.environ.get("OPSLOG_DIR")
        p = Path(env) if env else Path(__file__).resolve().parents[2] / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def rotate_if_stale(log_dir=None, today: date | None = None) -> bool:
    """换日首写轮转：curr.log 的创建日 ≠ 今天 → 改名定格为 ops-<创建日>.log。
    返回是否发生了轮转。旧文件原样保留 = 回滚底牌（§17.3）。"""
    d = _log_dir(log_dir)
    today = today or date.today()
    curr, marker = d / _CURR, d / _MARKER
    if not curr.exists():
        marker.write_text(today.isoformat(), encoding="ascii")
        return False
    created = marker.read_text(encoding="ascii").strip() if marker.exists() \
        else datetime.fromtimestamp(curr.stat().st_mtime).date().isoformat()
    if created == today.isoformat():
        return False
    target = d / _ARCHIVE.format(created.replace("-", ""))
    if target.exists():                       # 同日归档已存在（跨日多次轮转/残留）→ 加序号，绝不覆盖
        n = 1
        while (d / _ARCHIVE.format(f"{created.replace('-', '')}_{n}")).exists():
            n += 1
        target = d / _ARCHIVE.format(f"{created.replace('-', '')}_{n}")
    curr.rename(target)
    marker.write_text(today.isoformat(), encoding="ascii")
    return True


def format_line(code: str, key: str, fields: dict | None = None,
                when: datetime | None = None) -> str:
    """单行制：HH:MM:SS|code|key|k=v k=v。None/空值字段跳过（不落 'None' 字面量）。"""
    if code not in CODES:
        raise ValueError(f"非法事件码 {code!r}（允许：{'/'.join(sorted(CODES))}）")
    ts = (when or datetime.now()).strftime("%H:%M:%S")
    kv = " ".join(f"{k}={v}" for k, v in (fields or {}).items() if v is not None)
    return f"{ts}|{code}|{key}|{kv}".rstrip("|")


def log_event(code: str, key: str, fields: dict | None = None,
              log_dir=None, when: datetime | None = None) -> str:
    """写一条明细级事件到 logs/curr.log（先检查轮转）。返回所写行。"""
    d = _log_dir(log_dir)
    rotate_if_stale(d, when.date() if when else None)
    line = format_line(code, key, fields, when)
    with open(d / _CURR, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line


def log_summary(counts: dict, log_dir=None, when: datetime | None = None) -> str:
    """日报级聚合一行（U|code=daily）：只回答今天健康吗，禁止用于排查。"""
    return log_event("U", "daily", counts, log_dir, when)


def eod_backup(db_path, log_dir=None, today: date | None = None) -> Path:
    """EOD：DB 快照备份进当日目录（§17.3：logs/<YYYYMMDD>/vol.db）。
    shutil.copy 原子性足够（单机 SQLite，EOD 静默期执行）。"""
    import shutil
    d = _log_dir(log_dir)
    today = today or date.today()
    dst = d / today.strftime("%Y%m%d")
    dst.mkdir(parents=True, exist_ok=True)
    target = dst / Path(db_path).name
    if Path(db_path).exists():
        shutil.copy2(db_path, target)
    return target


def read_events(log_dir=None, code: str | None = None, key: str | None = None) -> list[str]:
    """排查辅助：读 curr.log（可选按 code/key 过滤），对齐 Runbook grep 用法。"""
    d = _log_dir(log_dir)
    fp = d / _CURR
    if not fp.exists():
        return []
    out = []
    for ln in fp.read_text(encoding="utf-8").splitlines():
        parts = ln.split("|")
        if code and (len(parts) < 2 or parts[1] != code):
            continue
        if key and (len(parts) < 3 or parts[2] != key):
            continue
        out.append(ln)
    return out
