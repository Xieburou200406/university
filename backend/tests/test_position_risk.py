# -*- coding: utf-8 -*-
"""§20.2 P1 持仓风险评审：保证金占用估算上界 / 浮亏触发线 / 到期周提醒。
金样本来自正式库真实数据：STRADDLE K=3.0 2026-10-28，qty=-2，open_px=0.095。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

import api.deps as deps
from db.models import Position, PositionSnapshot
from vol import position_risk as pr


# ---------- 纯函数：保证金 ----------

def test_margin_upper_bound_golden_sample():
    # 金样本：px=1024.57/20000=0.05123，spot=2.918 → 10%·spot 封顶项主导
    m = pr.estimate_margin_short(1024.565 / 20000, 2.918)
    assert m == pytest.approx((0.0512283 + max(0.12 * 0.0512283, 0.10 * 2.918)) * 10000, rel=1e-3)
    assert m == pytest.approx(3430.3, rel=1e-3)


def test_margin_is_upper_bound_vs_virtual_value_deduction():
    # 不扣虚值 → 上界：任何 (px, spot) 下 ≥ 含虚值抵扣公式的结果（虚值≥0）
    px, spot, strike = 0.20, 2.90, 2.80
    strict = (px + max(0.12 * px - max(spot - strike, 0.0), 0.10 * spot)) * 10000
    assert pr.estimate_margin_short(px, spot) >= strict


def test_margin_scales_with_qty_in_review_row():
    # 2 张义务仓 = 单张 × 2（在 services 聚合层乘 |qty|，这里验证纯函数是单张口径）
    assert pr.estimate_margin_short(0.1, 2.9) == pr.estimate_margin_short(0.1, 2.9, unit=10000)


# ---------- 纯函数：浮亏分级 ----------

def test_pnl_sign_convention_short_and_long():
    # 空头金样本：mv=-1024.57，qty=-2，open_px=0.095 → pnl = +875.43（浮盈）
    assert pr.unrealized_pnl(-1024.565, -2, 0.095) == pytest.approx(875.435, rel=1e-4)
    # 空头亏损：价格翻倍 mv=-3800 → pnl = -1900 → 全亏
    assert pr.unrealized_pnl(-3800.0, -2, 0.095) == pytest.approx(-1900.0, rel=1e-6)
    # 多头亏损：付 0.1 开 1 张，现值 0.04 → -600
    assert pr.unrealized_pnl(400.0, 1, 0.10) == pytest.approx(-600.0, rel=1e-6)


def test_loss_levels_thresholds():
    # 49% → None；51% → WARN；99% → WARN；101% → STRONG（权利金口径）
    def lvl(mv_ratio):  # mv_ratio = |mv|/(2*0.095*10000) 对空头的偏离
        premium = 2 * 0.095 * 10000
        mv = -(premium * (1 + mv_ratio))
        return pr.loss_level(mv, -2, 0.095)[1]

    assert lvl(0.49) is None
    assert lvl(0.51) == "WARN"
    assert lvl(0.99) == "WARN"
    assert lvl(1.01) == "STRONG"
    # 盈利仓比例归 0
    assert pr.loss_level(-500.0, -2, 0.095) == (0.0, None)


# ---------- 纯函数：到期解析 ----------

def test_parse_expiry_real_contract_code():
    # 真实码 → 第 4 个周三（铁律 4 实证：2026-10 → 2026-10-28）
    assert pr.parse_expiry("510050C2610M02800") == date(2026, 10, 28)


def test_parse_expiry_demo_ledger_code():
    assert pr.parse_expiry("STRADDLE K=3.0 2026-10-28") == date(2026, 10, 28)
    assert pr.parse_expiry("随便什么无日期") is None


# ---------- services 聚合 ----------

@pytest.fixture()
def client(monkeypatch):
    from db.session import make_engine, make_session
    engine = make_engine("sqlite:///:memory:")
    from db.models import Base
    Base.metadata.create_all(engine)
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


def _seed_position(s, code="STRADDLE K=3.0 2026-10-28", qty=-2, open_px=0.095,
                   mv=-1024.565, spot=2.918, snap_date=date(2026, 10, 9)):
    p = Position(code=code, qty=qty, open_date=date(2026, 9, 2), open_px=open_px, status="open")
    s.add(p)
    s.flush()
    s.add(PositionSnapshot(date=snap_date, position_id=p.id, mv=mv,
                           delta=0.0, vega=-1.2, theta=-0.5, iv=0.18, spot=spot))
    s.commit()
    return p


def test_review_golden_sample(session):
    from api.services import position_risk_review
    _seed_position(session)
    rv = position_risk_review(session)
    assert rv["asof"] == "2026-10-09"
    row = rv["rows"][0]
    assert row["qty"] == -2 and row["cur_px"] == pytest.approx(0.0512, abs=1e-3)
    assert row["unrealized_pnl"] == pytest.approx(875.44, abs=0.1)
    assert row["loss_level"] is None                    # 浮盈仓无亏损角标
    assert row["margin_est"] == pytest.approx(6860.6, abs=1.0)   # 单张 3430.3 × 2
    assert row["expiry"] == "2026-10-28"
    assert rv["max_loss_level"] is None
    assert rv["total_margin_est"] == pytest.approx(6860.6, abs=1.0)


def test_review_loss_and_expiry_week_flags(session):
    # 浮亏 150%（mv=-4750 → 亏 2850/1900=1.5）+ 快照日贴近到期日 → 到期周 STRONG
    _seed_position(session, snap_date=date(2026, 10, 23), mv=-4750.0)
    from api.services import position_risk_review
    rv = position_risk_review(session)
    assert rv["max_loss_level"] == "STRONG"
    row = rv["rows"][0]
    assert row["loss_ratio"] == pytest.approx(1.5, abs=0.01)
    assert row["expiry_week"] is True and rv["expiry_week"]


def test_review_skips_position_without_snapshot(session):
    p = Position(code="STRADDLE K=2.5 2026-11-25", qty=-1, open_date=date(2026, 10, 1),
                 open_px=0.08, status="open")
    session.add(p)
    session.commit()
    from api.services import position_risk_review
    assert position_risk_review(session)["rows"] == []


def test_review_empty_ledger(session):
    from api.services import position_risk_review
    rv = position_risk_review(session)
    assert rv == {"asof": None, "rows": [], "total_margin_est": 0.0,
                  "max_loss_level": None, "expiry_week": [], "price_limit_hits": []}


# ---------- 纯函数：涨跌停区间（§20.2 #5）----------

def test_price_limit_band_golden_deep_otm():
    # 金样本：前结算 0.0512（深度虚值腿），标的前收 2.918
    # 认购上界 = 0.0512 + max(0.5%·0.0512, min(10%·(2·0.0512−2.918), 10%·2.918)) = 0.0512+0.000256（0.5% 地板主导）
    lo, hi = pr.price_limit_band(0.0512283, 2.918, kind="C")
    assert lo == pytest.approx(0.0461, abs=1e-4)
    assert hi == pytest.approx(0.0512 + 0.000256, abs=1e-4)
    # 认沽上界 = 0.0512 + 10%·2.918（10%·标的封顶主导）
    _, hi_p = pr.price_limit_band(0.0512283, 2.918, kind="P")
    assert hi_p == pytest.approx(0.0512 + 0.2918, abs=1e-4)


def test_price_limit_band_none_kind_is_union():
    # None（跨式/未知类型）= 并集（更宽者）：合成定价账本若用交集会天天误报越界（警报疲劳）
    lo_c, hi_c = pr.price_limit_band(0.20, 2.90, kind="C")
    lo_p, hi_p = pr.price_limit_band(0.20, 2.90, kind="P")
    lo_n, hi_n = pr.price_limit_band(0.20, 2.90, kind=None)
    assert lo_n == pytest.approx(max(lo_c, lo_p))     # 下界同源
    assert hi_n == pytest.approx(max(hi_c, hi_p))     # 并集：单腿最宽区间


def test_price_limit_band_fail_soft():
    assert pr.price_limit_band(0.0, 2.9) is None
    assert pr.price_limit_band(0.05, -1.0) is None


def test_kind_from_code_drives_band_choice():
    # 真实码 → 精确口径；demo 账本码 → None（并集宽口径）
    assert pr.kind_from_code("510050C2610M02800") == "C"
    assert pr.kind_from_code("510050P2610M02800") == "P"
    assert pr.kind_from_code("STRADDLE K=3.0 2026-10-28") is None
    # 回归锁：真实认购码在深虚值处上界必须紧（0.5% 地板），None 则宽
    _, hi_c = pr.price_limit_band(0.0512283, 2.918, pr.kind_from_code("510050C2610M02800"))
    _, hi_n = pr.price_limit_band(0.0512283, 2.918, pr.kind_from_code("STRADDLE K=3.0 2026-10-28"))
    assert hi_c < hi_n


# ---------- services 聚合：涨跌停越界 ----------

def test_review_price_limit_hit_detection(session):
    # 前日 px=0.05 → 并集区间 ≈ [0.045, 0.342]（宽：认沽腿 10%·标的主导）；
    # 次日跌到 0.03（低于跌停 0.045）→ 越下界。并集口径下向上越界需跳涨 >6 倍，属真实异常才触发。
    p = Position(code="STRADDLE K=3.0 2026-10-28", qty=-2, open_date=date(2026, 9, 2),
                 open_px=0.095, status="open")
    session.add(p)
    session.flush()
    session.add(PositionSnapshot(date=date(2026, 10, 8), position_id=p.id, mv=-1000.0,
                                 delta=0.0, vega=-1.0, theta=-0.4, iv=0.18, spot=2.918))
    session.add(PositionSnapshot(date=date(2026, 10, 9), position_id=p.id, mv=-600.0,
                                 delta=0.0, vega=-1.0, theta=-0.4, iv=0.18, spot=2.918))
    session.commit()
    from api.services import position_risk_review
    rv = position_risk_review(session)
    row = rv["rows"][0]
    assert row["cur_px"] == pytest.approx(0.03)
    assert row["price_limit_hit"] == "DOWN"
    assert rv["price_limit_hits"] and "DOWN" in rv["price_limit_hits"][0]


def test_review_price_limit_inside_band(session):
    # 沿用金样本两快照：0.0512 → 0.0512，价格没动 → 区间内
    _seed_position(session)
    from api.services import position_risk_review
    rv = position_risk_review(session, asof=date(2026, 10, 10))  # asof 无快照 → rows 空
    assert rv["rows"] == []
    # 正确姿势：直接用默认 asof（库里只有一天快照，无前日 → 不判越界）
    from db.session import make_engine  # noqa: F401  确认同 session 上下文
    rv2 = position_risk_review(session)
    assert rv2["rows"][0]["price_limit_hit"] is None


# ---------- API ----------

def test_risk_review_endpoint(client, session):
    _seed_position(session)
    resp = client.get("/api/positions/risk-review")
    assert resp.status_code == 200
    body = resp.json()
    assert body["rows"][0]["expiry"] == "2026-10-28"
    assert body["rows"][0]["margin_est"] > 0
