# -*- coding: utf-8 -*-
"""M7-① 结构化事件日志（§17.1/§17.3）：单行制、两级粒度、daily/curr 轮转、管线埋点。"""
from datetime import date, datetime

import pandas as pd
import pytest

from vol import opslog


# ---------- 单行制格式 ----------

def test_format_line_golden():
    line = opslog.format_line("D", "panel", {"date": "20261008", "rows": 121, "src": "akshare"},
                              when=datetime(2026, 10, 14, 8, 30, 5))
    assert line == "08:30:05|D|panel|date=20261008 rows=121 src=akshare"
    # 数字段法：split('|') 后字段位置固定
    assert line.split("|")[1] == "D" and line.split("|")[2] == "panel"


def test_format_line_skips_none_and_strips_trailing():
    assert opslog.format_line("S", "signal", {"engine": "analog", "p": None},
                              when=datetime(2026, 10, 14, 9, 0, 0)) == "09:00:00|S|signal|engine=analog"
    assert opslog.format_line("X", "jump", {}, when=datetime(2026, 10, 14, 9, 0, 0)) == "09:00:00|X|jump"


def test_invalid_code_rejected():
    with pytest.raises(ValueError):
        opslog.format_line("Z", "bad", {})


# ---------- 轮转（daily/curr 同构：标记文件 + 改名定格）----------

def test_rotation_freezes_yesterday(tmp_path):
    d1, d2 = date(2026, 10, 13), date(2026, 10, 14)
    opslog.log_event("D", "panel", {"rows": 1}, log_dir=tmp_path, when=datetime(2026, 10, 13, 8, 0))
    assert (tmp_path / "curr.log").exists()
    assert opslog.rotate_if_stale(tmp_path, d1) is False      # 同日不轮转
    assert opslog.log_event("P", "pipeline", {}, log_dir=tmp_path,
                            when=datetime(2026, 10, 13, 15, 5)) and True
    # 换日首写 → curr.log 改名定格为 ops-20261013.log，新 curr.log 只含今天
    opslog.log_event("D", "panel", {"rows": 2}, log_dir=tmp_path, when=datetime(2026, 10, 14, 8, 30))
    assert (tmp_path / "ops-20261013.log").exists()
    archived = (tmp_path / "ops-20261013.log").read_text(encoding="utf-8")
    assert "rows=1" in archived and "rows=2" not in archived   # 旧目录定格 = 回滚底牌
    assert "rows=2" in (tmp_path / "curr.log").read_text(encoding="utf-8")
    # 已是今天 → 再写不轮转
    assert opslog.rotate_if_stale(tmp_path, d2) is False


def test_summary_goes_to_curr_with_u_code(tmp_path):
    line = opslog.log_summary({"signals": 3, "abstains": 1}, log_dir=tmp_path,
                              when=datetime(2026, 10, 14, 15, 10))
    assert line.startswith("15:10:00|U|daily|")
    assert "signals=3" in line


def test_eod_backup_into_dated_dir(tmp_path):
    db = tmp_path / "vol.db"
    db.write_bytes(b"sqlite-mock")
    target = opslog.eod_backup(db, log_dir=tmp_path, today=date(2026, 10, 14))
    assert target == tmp_path / "20261014" / "vol.db"
    assert target.read_bytes() == b"sqlite-mock"


def test_read_events_filter(tmp_path):
    opslog.log_event("D", "panel", {"rows": 1}, log_dir=tmp_path, when=datetime(2026, 10, 14, 8, 0))
    opslog.log_event("X", "forecast", {"reason": "abstain"}, log_dir=tmp_path,
                     when=datetime(2026, 10, 14, 8, 1))
    assert len(opslog.read_events(log_dir=tmp_path)) == 2
    assert all("|X|" in ln for ln in opslog.read_events(log_dir=tmp_path, code="X"))
    assert opslog.read_events(log_dir=tmp_path, key="panel") and \
        all("|panel|" in ln for ln in opslog.read_events(log_dir=tmp_path, key="panel"))


# ---------- 管线埋点（integration：monkeypatch 捕获，不写盘）----------

@pytest.fixture()
def session():
    from sqlalchemy import create_engine
    from db.session import make_session
    engine = create_engine("sqlite:///:memory:")
    from db.models import Base
    Base.metadata.create_all(engine)
    Session, _ = make_session(engine)
    s = Session()
    yield s
    s.close()


def _fake_panel(n=130):
    return pd.DataFrame({
        "date": [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)],
        "spot": [2.7 + 0.001 * i for i in range(n)],
        "iv_near": [0.25 + 0.0005 * (i % 10) for i in range(n)],
        "iv_next": [0.26] * n,
        "skew25": [0.01] * n,
        "rvol20": [0.2] * n,
    })


def test_pipeline_emits_structured_events(monkeypatch, session):
    import api.services as svc
    events: list[tuple] = []
    monkeypatch.setattr(svc.opslog, "log_event", lambda c, k, f=None: events.append((c, k, f or {})))
    summary = svc.compute_and_store_daily(session, svc.Settings(), fetch_df=_fake_panel())
    codes = [c for c, _, _ in events]
    assert "D" in codes and "P" in codes                       # 数据 + 管线完成
    assert codes.count("S") == 3                               # percentile/analog/ensemble 各一条
    s_events = [f for c, k, f in events if c == "S"]
    assert all("verdict" in f and "valid_until" in f for f in s_events)   # 负记录：观望也记
    # P| 管线事件带耗时（ms 字段存在）
    p_event = [f for c, k, f in events if c == "P"][0]
    assert "ms" in p_event and p_event["ok"] == 1


def test_pipeline_failure_emits_x_event(monkeypatch, session):
    import api.services as svc
    events: list[tuple] = []
    monkeypatch.setattr(svc.opslog, "log_event", lambda c, k, f=None: events.append((c, k, f or {})))
    with pytest.raises(RuntimeError):
        svc.compute_and_store_daily(session, svc.Settings(), fetch_df=pd.DataFrame())
    x_events = [f for c, k, f in events if c == "X"]
    assert x_events and x_events[0]["reason"] == "no_panel"     # 负记录：为什么没信号
