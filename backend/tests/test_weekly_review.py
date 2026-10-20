# -*- coding: utf-8 -*-
"""M6-③ 命中率周度复核：落 ReviewLog（同周覆盖）、低样本 fail-soft、API 端到端。"""
from datetime import date

import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select


def _memory_session():
    from sqlalchemy import create_engine
    from sqlalchemy.pool import StaticPool
    from db.session import make_session
    from db.models import Base
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    SM, _ = make_session(eng)
    return SM()


class _FakePanel(dict):
    """dict 装载 pd.Series，支持 df["col"] 下标与 .median()。"""

    @property
    def empty(self):
        return False


def _panel(n_days, iv_fn):
    days = pd.date_range("2026-05-01", periods=n_days).strftime("%Y-%m-%d").tolist()
    return _FakePanel(
        iv_near=pd.Series([iv_fn(i) for i in range(n_days)]),
        spot=pd.Series([2.9] * n_days),
        iv_next=pd.Series([0.11] * n_days),
        skew25=pd.Series([0.01] * n_days),
        rvol20=pd.Series([0.1] * n_days),
        date=pd.Series(days),
    )


def _patch(monkeypatch, panel, ana, win, hor):
    import m2_run
    import m3_run
    monkeypatch.setattr(m2_run, "build_daily_history", lambda: panel)
    monkeypatch.setattr(m3_run, "analog_states", lambda df, **kw: ana)
    monkeypatch.setattr(m2_run, "WINDOW", win)
    monkeypatch.setattr(m2_run, "HORIZON", hor)


def test_weekly_review_records_and_overwrites(monkeypatch):
    """复核落 ReviewLog；同周重跑覆盖不重复；edge 正常。"""
    import api.services as svc
    from db.models import ReviewLog
    s = _memory_session()
    n, win, hor = 160, 120, 10
    panel = _panel(n, lambda i: 0.10 + 0.0002 * ((i * 7) % 40))
    ana = [0] * win + [1 if i % 2 else -1 for i in range(win, n - hor)]
    _patch(monkeypatch, panel, ana, win, hor)

    r1 = svc.run_weekly_review(s, asof=date(2026, 10, 16))
    assert r1["recorded"] is True and r1["week"] == "2026-W42"
    assert r1["samples"] >= 30
    rows = s.execute(select(ReviewLog)).scalars().all()
    assert len(rows) == 1
    r2 = svc.run_weekly_review(s, asof=date(2026, 10, 16))       # 同周重跑 → 覆盖
    rows = s.execute(select(ReviewLog)).scalars().all()
    assert len(rows) == 1 and rows[0].hit_rate == r2["hit_rate"]
    assert rows[0].by_regime_json["edge"] == pytest.approx(r2["edge"], abs=1e-6)


def test_weekly_review_low_samples_skips(monkeypatch):
    """样本 < 30 → fail-soft 跳过，不落库。"""
    import api.services as svc
    from db.models import ReviewLog
    s = _memory_session()
    _patch(monkeypatch, _panel(20, lambda i: 0.1), [1] * 20, 5, 2)
    r = svc.run_weekly_review(s, asof=date(2026, 10, 16))
    assert r["recorded"] is False and "样本" in r["reason"]
    assert s.execute(select(ReviewLog)).scalars().all() == []


def test_weekly_review_warn_streak(monkeypatch):
    """连续 4 周跌破基准 → warn=True；本周反超 → warn 清零。"""
    import api.services as svc
    from db.models import ReviewLog
    s = _memory_session()
    n, win, hor = 200, 120, 10
    # 引擎恒与实际反向：analog=+1 时 IV 必跌 → hit_rate ≈ 0 << baseline
    panel = _panel(n, lambda i: 0.15 - 0.0001 * i)
    ana = [0] * win + [1] * (n - win - hor)
    _patch(monkeypatch, panel, ana, win, hor)

    for d in (date(2026, 9, 25), date(2026, 10, 2), date(2026, 10, 9)):
        svc.run_weekly_review(s, asof=d)
    rows = s.execute(select(ReviewLog).order_by(ReviewLog.id)).scalars().all()
    assert all(r.warn is False for r in rows)                    # <4 周不告警
    r = svc.run_weekly_review(s, asof=date(2026, 10, 16))        # 第 4 周
    assert r["warn"] is True
    # 本周反超 → warn 清零：IV 每 10 天翻转一次（actuals 涨跌各半 → baseline≈0.5），
    # 引擎方向随之全对 → hit_rate≈1.0 > baseline
    panel2 = _panel(n, lambda i: 0.15 + 0.001 * ((i // 10) % 2))
    _patch(monkeypatch, panel2, [0] * win + [1 if (i // 10) % 2 == 0 else -1
                                            for i in range(win, n - hor)], win, hor)
    r2 = svc.run_weekly_review(s, asof=date(2026, 10, 16))
    assert r2["warn"] is False and r2["hit_rate"] > r2["baseline"]


@pytest.fixture()
def client(monkeypatch):
    import api.deps as deps
    from db.session import make_engine, make_session
    engine = make_engine("sqlite:///:memory:")
    from db.models import Base
    Base.metadata.create_all(engine)
    Session, _ = make_session(engine)
    monkeypatch.setattr(deps, "init_engine", lambda: Session)
    from api.app import create_app
    return TestClient(create_app())


def test_review_api_end_to_end(client, monkeypatch):
    """POST /api/reviews/run → GET /api/reviews 看到记录。"""
    import api.services as svc
    from db.models import ReviewLog

    def _fake(session, asof=None):
        row = ReviewLog(week="2026-W42", sample_n=88, hit_rate=0.62, baseline=0.55,
                        by_regime_json={"edge": 0.07}, warn=False)
        session.add(row)
        session.commit()
        return {"recorded": True, "week": "2026-W42", "samples": 88,
                "hit_rate": 0.62, "baseline": 0.55, "edge": 0.07, "warn": False}

    monkeypatch.setattr(svc, "run_weekly_review", _fake)
    r = client.post("/api/reviews/run")
    assert r.status_code == 200 and r.json()["recorded"] is True
    r = client.get("/api/reviews")
    assert r.status_code == 200 and r.json()[0]["week"] == "2026-W42"
