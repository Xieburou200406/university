# -*- coding: utf-8 -*-
"""M7-②：快照只追加约束（§17.2）+ 差分归因唯一实现 + IV 跳变留痕（§17.6）。"""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from db.models import Base, DailyMetric, Position, PositionSnapshot
from db.session import make_engine, make_session
from vol import position_risk as pr


@pytest.fixture()
def session(monkeypatch):
    monkeypatch.setenv("OPSLOG_DIR", "logs_test_m72")
    eng = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    SM, _ = make_session(eng)
    s = SM()
    yield s
    s.close()


# ---------- ① 只追加：UPDATE 直接报错（制度变机制） ----------

def test_snapshot_update_is_blocked(session):
    p = Position(code="STRADDLE K=3.0 2026-10-28", qty=-2, open_date=date(2026, 10, 8),
                 open_px=0.095, status="open")
    session.add(p)
    session.flush()
    session.add(PositionSnapshot(date=date(2026, 10, 8), position_id=p.id, mv=-1900.0,
                                 delta=0.0, vega=-1.0, theta=-0.4, iv=0.18, spot=2.918))
    session.commit()
    row = session.execute(select(PositionSnapshot).limit(1)).scalar_one()
    row.mv = -999.0                                        # 篡改历史
    with pytest.raises(Exception, match="append-only"):
        session.commit()


def test_snapshot_insert_still_works_after_block(session):
    p = Position(code="X 2026-10-28", qty=1, open_date=date(2026, 10, 8),
                 open_px=0.1, status="open")
    session.add(p)
    session.flush()
    for d, mv in [(date(2026, 10, 8), 100.0), (date(2026, 10, 9), 120.0)]:
        session.add(PositionSnapshot(date=d, position_id=p.id, mv=mv, delta=0.0,
                                     vega=0.0, theta=0.0, iv=0.2, spot=2.9))
    session.commit()                                       # 追加路径不受触发器影响
    assert session.execute(select(PositionSnapshot)).scalars().all().__len__() == 2


# ---------- ② 差分归因唯一实现：golden + 数学闭合 + "加总刷新值必须被识破" ----------

def test_pnl_attribution_golden():
    daily, cum = pr.pnl_daily_attribution([0.0, -100.0, -300.0, -250.0])
    assert daily == pytest.approx([0.0, -100.0, -200.0, 50.0])
    assert cum == pytest.approx([0.0, -100.0, -300.0, -250.0])


def test_pnl_attribution_closes_within_one_tick():
    mvs = [-1900.0, -1024.57, -875.43, -1330.21]
    _, cum = pr.pnl_daily_attribution(mvs)
    assert cum[-1] == pytest.approx(mvs[-1] - mvs[0], abs=1e-6)   # 伸缩恒等式 = ±1 tick 结案


def test_naive_sum_of_refresh_values_differs_and_is_wrong():
    # 铁律反例：把累计市值快照直接加总 ≠ 差分归因——差分才是盈亏，加总是"市值水平之和"
    mvs = [0.0, -100.0, -300.0, -250.0]
    _, cum = pr.pnl_daily_attribution(mvs)
    assert sum(mvs) != pytest.approx(cum[-1])
    assert cum[-1] == pytest.approx(-250.0)                # 正确答案：期间净变动


def test_pnl_attribution_empty_and_single():
    assert pr.pnl_daily_attribution([]) == ([], [])
    assert pr.pnl_daily_attribution([5.0]) == ([0.0], [0.0])


# ---------- ③ IV 跳变（§17.6） ----------

def _seed_metrics(session, vals):
    d0 = date(2026, 10, 6)
    for i, v in enumerate(vals):
        session.add(DailyMetric(date=d0 + timedelta(days=i), spot=2.9, atm_iv=0.2,
                                iv_pct_40=0.5, iv_pct_120=v))
    session.flush()


def test_iv_jump_none(session):
    from api.services import _iv_jump_review
    _seed_metrics(session, [0.50, 0.52])
    d = date(2026, 10, 7)
    out = _iv_jump_review(session, d, {})
    assert out["iv_jump"] is False and out["iv_degrade"] is False


def test_iv_jump_single_day_logged(session):
    from api.services import _iv_jump_review
    _seed_metrics(session, [0.50, 0.85])                   # 单日 +0.35 > 0.30
    out = _iv_jump_review(session, date(2026, 10, 7), {})
    assert out["iv_jump"] is True and out["iv_degrade"] is False
    assert out["iv_delta"] == pytest.approx(0.35)


def test_iv_jump_consecutive_days_degrade(session):
    from api.services import _iv_jump_review
    _seed_metrics(session, [0.50, 0.85, 0.50])             # 两个连续跳变日（+0.35 / -0.35）
    out = _iv_jump_review(session, date(2026, 10, 8), {})
    assert out["iv_jump"] is True and out["iv_degrade"] is True


def test_iv_jump_missing_values_fail_soft(session):
    from api.services import _iv_jump_review
    session.add(DailyMetric(date=date(2026, 10, 7), spot=2.9, atm_iv=0.2, iv_pct_120=None))
    session.flush()
    out = _iv_jump_review(session, date(2026, 10, 7), {})  # 缺值 → 不判跳变，不崩
    assert out["iv_jump"] is False
