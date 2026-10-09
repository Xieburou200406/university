# -*- coding: utf-8 -*-
"""§18.6 建议生命周期：滞留-采纳闭环。
T 收盘生成 pending 卡 → T+1 有效期内滞留再呈现（/advice/today）
→ 采纳（留痕）/忽略；过期未处理自动清算为 expired（留痕不删）。"""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

import api.deps as deps
from db.models import AdviceCard, Signal


def _make_memory_engine():
    import api.deps as _deps  # noqa: F401
    from db.session import make_engine
    return make_engine("sqlite:///:memory:")


@pytest.fixture()
def client(monkeypatch):
    engine = _make_memory_engine()
    from db.models import Base
    Base.metadata.create_all(engine)
    from db.session import make_session
    Session, _ = make_session(engine)
    monkeypatch.setattr(deps, "init_engine", lambda: Session)
    from api.app import create_app
    return TestClient(create_app())


@pytest.fixture()
def session(client):
    Session = deps.init_engine()
    s = Session()
    yield s
    s.close()


def _seed_signal(s, valid_until):
    sig = Signal(date=valid_until, engine="percentile", signal="SHORT_VOL",
                 confidence=0.9, valid_until=valid_until,
                 detail_json={"iv_pct": 0.8})
    s.add(sig)
    s.commit()
    return sig


def _seed_card(s, sig, status="pending"):
    ac = AdviceCard(signal_id=sig.id, verdict_json={"verdict": "PASS", "checks": []},
                    card_json={"title": "测试建议"}, status=status)
    s.add(ac)
    s.commit()
    return ac


# ---- 有效期内：滞留再呈现 + 采纳留痕 ----

def test_today_returns_pending_card_within_validity(client, session):
    sig = _seed_signal(session, date(2026, 10, 12))          # 周一有效
    _seed_card(session, sig)
    r = client.get("/api/advice/today")
    assert r.status_code == 200
    body = r.json()
    assert body["card"]["status"] == "pending"
    # 重复查询 = 滞留再呈现：一直给到处理为止
    assert client.get("/api/advice/today").json()["card"]["id"] == body["card"]["id"]


def test_adopt_records_timestamp_then_stops_presenting(client, session):
    sig = _seed_signal(session, date(2026, 10, 12))
    ac = _seed_card(session, sig)
    r = client.post(f"/api/advice/{ac.id}/adopt")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "adopted" and body["adopted_at"] is not None
    # 采纳后不再滞留呈现
    assert client.get("/api/advice/today").json()["card"] is None
    # 重复采纳 → 409
    assert client.post(f"/api/advice/{ac.id}/adopt").status_code == 409


def test_dismiss_stops_presenting(client, session):
    sig = _seed_signal(session, date(2026, 10, 12))
    ac = _seed_card(session, sig)
    assert client.post(f"/api/advice/{ac.id}/dismiss").status_code == 200
    assert client.get("/api/advice/today").json()["card"] is None
    assert client.post(f"/api/advice/{ac.id}/dismiss").status_code == 409


# ---- 过期：自动清算留痕 + 过期卡不可采纳 ----

def test_expired_card_auto_cleared_on_today_query(client, session):
    sig = _seed_signal(session, date(2026, 9, 1))            # 明确过期
    ac = _seed_card(session, sig)
    assert client.get("/api/advice/today").json()["card"] is None
    session.expire_all()
    row = session.get(AdviceCard, ac.id)
    assert row.status == "expired"                            # 留痕不删
    assert client.post(f"/api/advice/{ac.id}/adopt").status_code == 409


def test_adopt_nonexistent_card_404(client, session):
    assert client.post("/api/advice/99999/adopt").status_code == 404
    assert client.post("/api/advice/99999/dismiss").status_code == 404


def test_newest_pending_card_wins(client, session):
    """两张 pending：今日呈现最新一张（id 大者）。"""
    sig = _seed_signal(session, date(2026, 10, 12))
    old = _seed_card(session, sig)
    new = _seed_card(session, sig)
    assert client.get("/api/advice/today").json()["card"]["id"] == new.id
    client.post(f"/api/advice/{new.id}/adopt")
    # 新卡采纳后，旧卡（仍在有效期）顶上——一直给到采纳
    assert client.get("/api/advice/today").json()["card"]["id"] == old.id
