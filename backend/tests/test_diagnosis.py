# -*- coding: utf-8 -*-
"""M7-③：排查六步一键体检（§17.4）。"""
from datetime import date, timedelta

import pytest

from db.models import (Base, DailyMetric, OpenForecast, PipelineRun, Position,
                       PositionSnapshot, RiskBudget)
from db.session import make_engine, make_session


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setenv("OPSLOG_DIR", "logs_test_m73")
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    SM, _ = make_session(eng)
    s = SM()
    yield s
    s.close()


def test_empty_db_no_crash(session):
    from api.services import run_diagnosis
    rep = run_diagnosis(session)
    assert len(rep["steps"]) == 6
    assert rep["all_ok"] is False                      # 空库：步1-4 都不 ok
    assert [s["step"] for s in rep["steps"]] == [1, 2, 3, 4, 5, 6]


def test_happy_path_all_ok(session):
    from api.services import run_diagnosis
    d0 = date(2026, 10, 7)
    session.add(PipelineRun(status="ok", started_at=None, rows_fetched=1, error="ok"))
    session.add(DailyMetric(date=d0, spot=2.9, atm_iv=0.2, iv_pct_40=0.5,
                            iv_pct_120=0.55, skew_25d=-0.01, momentum_20d=0.02))
    session.add(OpenForecast(date=d0, model_ver="lr-irls-v1", p_up_raw=0.6,
                             p_up_cal=0.6, abstain=False, reason=None, features_json={}))
    session.add(RiskBudget(date=d0, lambda_level="balanced", target_vega=1.0, target_vega_lo=0.0,
                           target_vega_hi=2.0, target_delta=0.0, target_delta_lo=-1.0,
                           target_delta_hi=1.0, cur_vega=0.5, cur_delta=0.0, cvar5=100.0,
                           confidence="MID", degraded=False))
    p = Position(code="STRADDLE K=3.0 2026-10-28", qty=-2, open_date=d0, open_px=0.095, status="open")
    session.add(p)
    session.flush()
    for i, mv in enumerate([-1900.0, -1700.0, -1810.0]):
        session.add(PositionSnapshot(date=d0 + timedelta(days=i), position_id=p.id, mv=mv,
                                     delta=0.0, vega=-1.0, theta=-0.4, iv=0.18, spot=2.918))
    session.commit()
    rep = run_diagnosis(session)
    by_step = {s["step"]: s for s in rep["steps"]}
    assert by_step[1]["ok"] and by_step[2]["ok"] and by_step[3]["ok"] and by_step[4]["ok"]
    assert by_step[6]["ok"] is True                    # 伸缩恒等式闭合
    assert rep["all_ok"] is True
    assert "最大阶跃" in by_step[5]["detail"] and "+200.00" in by_step[5]["detail"]


def test_abstain_and_degraded_flagged(session):
    from api.services import run_diagnosis
    d0 = date(2026, 10, 7)
    session.add(OpenForecast(date=d0, model_ver="lr-irls-v1", p_up_raw=None, p_up_cal=None,
                             abstain=True, reason="有效样本仅 0 日", features_json=None))
    session.add(RiskBudget(date=d0, lambda_level="balanced", target_vega=0.0, target_vega_lo=0.0,
                           target_vega_hi=0.0, target_delta=0.0, target_delta_lo=0.0,
                           target_delta_hi=0.0, cur_vega=0.0, cur_delta=0.0, cvar5=None,
                           confidence="LOW", degraded=True,
                           notes_json={"reason": "预算管线异常（fail-closed）: 面板不可用"}))
    session.commit()
    rep = run_diagnosis(session)
    by_step = {s["step"]: s for s in rep["steps"]}
    assert by_step[3]["ok"] is False and "弃权" in by_step[3]["detail"]
    assert by_step[4]["ok"] is False and "降级" not in by_step[4]["detail"]
    assert "fail-closed" in by_step[4]["detail"]
    assert rep["all_ok"] is False
