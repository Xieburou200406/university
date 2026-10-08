# -*- coding: utf-8 -*-
"""M5-③ schema 往返测试：建表 → 插入 → 约束 → 查询（内存库，不碰 data/vol.db）。"""
from datetime import date, datetime

import pytest
from sqlalchemy import select

from db.models import (AdviceCard, DailyMetric, ExecutionLog, PipelineRun, Position,
                       PositionReconcile, PositionSnapshot, ReviewLog, Signal)
from db.session import make_engine, make_session


@pytest.fixture()
def session():
    engine = make_engine("sqlite:///:memory:")
    from db.models import Base
    Base.metadata.create_all(engine)
    Session, _ = make_session(engine)
    s = Session()
    yield s
    s.close()


def test_full_roundtrip(session):
    # 每日指标 + 信号 + 建议卡
    session.add(DailyMetric(date=date(2026, 10, 8), spot=2.71, atm_iv=0.273,
                            iv_pct_120=0.542, curve_model="SVI", curve_r2=0.9899))
    sig = Signal(date=date(2026, 10, 8), engine="analog", signal="NEUTRAL",
                 confidence=0.0, valid_until=date(2026, 10, 9), detail_json={"k": 20})
    session.add(sig)
    session.flush()
    session.add(AdviceCard(signal_id=sig.id, card_json={"note": "仅供参考"}))

    # 持仓 + 快照
    pos = Position(code="510050C2610M02700", qty=-1, open_date=date(2026, 9, 1), open_px=0.031)
    session.add(pos)
    session.flush()
    session.add(PositionSnapshot(date=date(2026, 10, 8), position_id=pos.id,
                                 mv=-310.0, delta=-0.5, vega=-2.4, theta=-365.0,
                                 iv=0.273, spot=2.71))
    session.add(ReviewLog(week="2026-W41", sample_n=360, hit_rate=0.808, baseline=0.547,
                          by_regime_json={"high_iv": 0.80}, warn=False))
    session.add(PipelineRun(status="ok", rows_fetched=580))
    session.add(PositionReconcile(date=date(2026, 10, 8), source="manual",
                                  per_contract_json={}, status="all_match"))
    session.add(ExecutionLog(signal_id=sig.id, acted_at=datetime.now(),
                             filled_px=0.032, slippage_bp=32.0))
    session.commit()

    # 查回并校验关键量纲字段
    m = session.scalar(select(DailyMetric).where(DailyMetric.date == date(2026, 10, 8)))
    assert m.atm_iv == 0.273 and m.curve_r2 == 0.9899
    sig2 = session.scalar(select(Signal))
    assert sig2.valid_until == date(2026, 10, 9)   # T+1（铁律 8）
    pos2 = session.scalar(select(Position))
    assert pos2.qty == -1 and pos2.status == "open"


def test_snapshot_unique_constraint(session):
    """同一 (date, position_id) 只能有一条快照 —— 归因差分的口径保障。"""
    import sqlalchemy.exc
    pos = Position(code="X", qty=1, open_date=date(2026, 1, 1), open_px=0.01)
    session.add(pos)
    session.flush()
    session.add(PositionSnapshot(date=date(2026, 1, 2), position_id=pos.id,
                                 mv=1, delta=0, vega=0, theta=0, iv=0.2, spot=2.7))
    session.commit()
    session.add(PositionSnapshot(date=date(2026, 1, 2), position_id=pos.id,
                                 mv=2, delta=0, vega=0, theta=0, iv=0.2, spot=2.7))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        session.commit()
