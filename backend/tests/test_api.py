# -*- coding: utf-8 -*-
"""M5-④ API 壳层测试：内存库 + TestClient，不碰 data/vol.db、不联网。
覆盖：health / metrics / signals / advice 风控链（PASS·WARN·REJECT 三态）/ positions / pipeline 防重入。"""
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

import api.deps as deps
from db.models import (AdviceCard, DailyMetric, PipelineRun, Position,
                       PositionSnapshot, Signal)


@pytest.fixture()
def client(monkeypatch):
    engine = _make_memory_engine()
    from db.models import Base
    Base.metadata.create_all(engine)
    from db.session import make_session
    Session, _ = make_session(engine)
    # 覆盖依赖：让 app 用内存库
    monkeypatch.setattr(deps, "init_engine", lambda: Session)
    from api.app import create_app
    return TestClient(create_app())


def _make_memory_engine():
    import api.deps as _deps  # noqa: F401
    from db.session import make_engine
    return make_engine("sqlite:///:memory:")


def _seed_basic(session):
    session.add(DailyMetric(date=date(2026, 10, 7), spot=2.70, atm_iv=0.27, iv_pct_120=0.52))
    session.add(DailyMetric(date=date(2026, 10, 8), spot=2.71, atm_iv=0.273, iv_pct_120=0.542))
    sig = Signal(date=date(2026, 10, 8), engine="percentile", signal="SHORT_VOL",
                 confidence=0.9, valid_until=date(2026, 10, 9),
                 detail_json={"iv_pct": 0.542, "confirm_needed": 0})
    session.add(sig)
    session.flush()
    return sig


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["advice_only"] is True and body["tables"] == 11


def test_metrics_list_and_detail(client):
    Session = deps.init_engine()
    s = Session()
    _seed_basic(s)
    s.commit()
    assert client.get("/api/metrics").status_code == 200
    r = client.get("/api/metrics/2026-10-08")
    assert r.status_code == 200 and r.json()["atm_iv"] == pytest.approx(0.273)
    assert client.get("/api/metrics/2000-01-01").status_code == 404
    s.close()


def test_signal_list_and_valid_only(client, monkeypatch):
    Session = deps.init_engine()
    s = Session()
    sig = _seed_basic(s)
    # 过期信号一条
    s.add(Signal(date=date(2026, 9, 1), engine="analog", signal="NEUTRAL",
                 valid_until=date(2026, 9, 2)))
    s.commit()
    r = client.get("/api/signals")
    assert r.status_code == 200 and len(r.json()) == 2
    r = client.get("/api/signals", params={"valid_only": True})
    ids = [x["id"] for x in r.json()]
    assert sig.id in ids and all(x["signal"] for x in r.json())
    r = client.get(f"/api/signals/{sig.id}")
    assert r.status_code == 200 and r.json()["engine"] == "percentile"
    s.close()


def test_advice_generate_pass(client):
    Session = deps.init_engine()
    s = Session()
    sig = _seed_basic(s)
    s.commit()
    r = client.post(f"/api/advice/generate/{sig.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "PASS"                    # 无持仓 → 敞口为 0，风控全过
    assert {c["name"] for c in body["risk_checks"]} == {"signal_valid_until", "vega_limit", "delta_limit"}
    assert "不自动下单" in body["card"]["card_json"]["disclaimer"]
    s.close()


def test_advice_generate_warn_vega(client):
    Session = deps.init_engine()
    s = Session()
    sig = _seed_basic(s)
    pos = Position(code="510050C2610M02700", qty=-1, open_date=date(2026, 9, 1), open_px=0.031)
    s.add(pos)
    s.flush()
    s.add(PositionSnapshot(date=date(2026, 10, 8), position_id=pos.id, mv=-310.0,
                           delta=-16000.0, vega=-900.0, theta=-365.0, iv=0.273, spot=2.71))
    s.commit()
    r = client.post(f"/api/advice/generate/{sig.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["verdict"] == "WARN"                    # vega 900 > 881 且 delta 16000 > 15000
    names = {c["name"]: c["passed"] for c in body["risk_checks"]}
    assert names["vega_limit"] is False and names["delta_limit"] is False
    assert "限额" in body["card"]["card_json"]["action"]
    s.close()


def test_advice_generate_reject_expired(client):
    Session = deps.init_engine()
    s = Session()
    sig = Signal(date=date(2026, 9, 1), engine="percentile", signal="LONG_VOL",
                 valid_until=date(2026, 9, 2))          # 已过期
    s.add(sig)
    s.commit()
    r = client.post(f"/api/advice/generate/{sig.id}")
    assert r.status_code == 200
    assert r.json()["verdict"] == "REJECT"              # 铁律 8：过期作废
    s.close()


def test_advice_404(client):
    assert client.post("/api/advice/generate/99999").status_code == 404


def test_positions_create_close_and_snapshots(client):
    Session = deps.init_engine()
    s = Session()
    r = client.post("/api/positions", json={"code": "510050P2610M02500", "qty": 1,
                                            "open_date": "2026-10-08", "open_px": 0.02})
    assert r.status_code == 201
    pid = r.json()["id"]
    pos = s.get(Position, pid)
    s.add(PositionSnapshot(date=date(2026, 10, 8), position_id=pos.id, mv=200.0,
                           delta=0.4, vega=2.1, theta=-300.0, iv=0.27, spot=2.71))
    s.commit()
    assert client.get(f"/api/positions/{pid}/snapshots").status_code == 200
    r = client.post(f"/api/positions/{pid}/close", json={"close_date": "2026-10-01", "close_px": 0.03})
    assert r.status_code == 422                          # 平仓早于开仓
    r = client.post(f"/api/positions/{pid}/close", json={"close_date": "2026-10-09", "close_px": 0.03})
    assert r.status_code == 200 and r.json()["status"] == "closed"
    r = client.post(f"/api/positions/{pid}/close", json={"close_date": "2026-10-09", "close_px": 0.03})
    assert r.status_code == 409                          # 重复平仓
    s.close()


def test_pipeline_reentry_lock_and_run(client, monkeypatch):
    """防重入：已有 running → 409；失败 → 502 + failed 记录；成功 → ok 且信号落库（注入假面板，不联网）。"""
    import pandas as pd

    Session = deps.init_engine()
    s = Session()
    s.add(PipelineRun(status="running"))
    s.commit()
    r = client.post("/api/pipeline/run")
    assert r.status_code == 409                       # 防重入锁（铁律 9：单一入口）
    s.execute(PipelineRun.__table__.delete())
    s.commit()

    import api.services as svc
    real_compute = svc.compute_and_store_daily   # 任何 patch 之前捕获原函数
    # 第一次：管线抛错（fail-closed）→ 502，且补记 failed 流水
    monkeypatch.setattr(svc, "compute_and_store_daily",
                        lambda session, settings, fetch_df=None: (_ for _ in ()).throw(RuntimeError("面板不可用")))
    r = client.post("/api/pipeline/run")
    assert r.status_code == 502
    assert any(x["status"] == "failed" for x in client.get("/api/pipeline/runs").json())

    # 第二次：注入假面板 → 成功
    n = 130
    df = pd.DataFrame({
        "date": [f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}" for i in range(n)],
        "spot": [2.7 + 0.001 * i for i in range(n)],
        "iv_near": [0.25 + 0.0005 * (i % 10) for i in range(n)],
        "iv_next": [0.26] * n,
        "skew25": [0.01] * n,
        "rvol20": [0.2] * n,
    })
    monkeypatch.setattr(svc, "compute_and_store_daily",
                        lambda session, settings, fetch_df=None:
                        real_compute(session, settings, fetch_df=df))
    r = client.post("/api/pipeline/run")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok"
    sigs = s.execute(select(Signal)).scalars().all()
    engines = {x.engine for x in sigs}
    assert {"percentile", "analog", "ensemble"} <= engines
    runs = client.get("/api/pipeline/runs").json()
    assert any(x["status"] == "ok" for x in runs)
    s.close()


def test_forecast_and_budget_endpoints(client):
    """§13/§14 端点：无记录 → 404；种数据后 next-open / today 可读；晨检只写 morning_adj。"""
    from db.models import OpenForecast, RiskBudget
    Session = deps.init_engine()
    s = Session()
    r = client.get("/api/forecast/next-open")
    assert r.status_code == 404
    r = client.get("/api/riskbudget/today")
    assert r.status_code == 404
    s.add(OpenForecast(date=date(2026, 10, 8), model_ver="lr-irls-v1", p_up_raw=0.62,
                       p_up_cal=0.63, abstain=False, reason="OK",
                       features_json={"momentum_20d": 0.02}))
    s.add(RiskBudget(date=date(2026, 10, 8), lambda_level="balanced", target_vega=-500.0,
                     target_vega_lo=-625.0, target_vega_hi=-375.0, target_delta=0.0,
                     cur_vega=-88.0, cur_delta=0.0, cvar5=-210.0, confidence="MID",
                     degraded=False, notes_json={}))
    s.commit()
    r = client.get("/api/forecast/next-open")
    assert r.status_code == 200 and r.json()["p_up_cal"] == pytest.approx(0.63)
    r = client.get("/api/forecast/history")
    assert r.status_code == 200 and len(r.json()) == 1
    r = client.post("/api/forecast/morning-check/2026-10-08", json={"p_up_observed": 0.58})
    assert r.status_code == 200
    body = r.json()
    assert body["morning_adj"] == pytest.approx(0.58) and body["p_up_cal"] == pytest.approx(0.63)
    assert client.post("/api/forecast/morning-check/2000-01-01", json={"p_up_observed": 0.5}).status_code == 404
    r = client.get("/api/riskbudget/today")
    assert r.status_code == 200
    b = r.json()
    assert b["target_vega"] == -500.0 and b["confidence"] == "MID" and b["degraded"] is False
    s.close()
