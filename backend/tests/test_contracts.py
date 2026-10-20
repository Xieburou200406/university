# -*- coding: utf-8 -*-
"""铁律 1/4 用例：行权价 ÷1000、到期日=第4个周三。"""
import pytest

from vol.contracts import parse_contract_code, expiry_from_code


def test_strike_scale():
    """铁律 1：02700 = 2.700 元（÷1000，不是 ÷10000）。"""
    spec = parse_contract_code("510050C2610M02700")
    assert spec.strike == 2.7
    assert spec.underlying == "510050"
    assert spec.cp == "C"


def test_expiry_fourth_wednesday():
    """铁律 4：到期日 = 到期月第 4 个周三。2026-10 → 2026-10-28。"""
    assert expiry_from_code("202610") == __import__("datetime").date(2026, 10, 28)
    assert expiry_from_code("202612") == __import__("datetime").date(2026, 12, 23)
    spec = parse_contract_code("510050P2612M02500")
    assert spec.strike == 2.5
    assert spec.expiry == __import__("datetime").date(2026, 12, 23)


def test_tier_label():
    spec = parse_contract_code("510050C2610M02700")
    assert spec.tier_label(2.7) == "ATM"
    assert spec.tier_label(3.0).startswith("实值")   # strike 2.7 < 现价 3.0 → Call 实值
    assert spec.tier_label(2.4).startswith("虚值")   # strike 2.7 > 现价 2.4 → Call 虚值


def test_invalid_code_raises():
    with pytest.raises(ValueError):
        parse_contract_code("NOT_A_CODE")


def test_sse_etf_code_tolerant_suffix():
    """数据层解析必须容忍尾部除权后缀（无 $ 尾锚）——加尾锚会让当日截面整表解析失败。"""
    from vol.contracts import parse_sse_etf_code
    assert parse_sse_etf_code("510050C2610M02700")["strike"] == 2.7
    assert parse_sse_etf_code("510050C2610M02700A")["strike"] == 2.7
    assert parse_sse_etf_code("510050C2610A02700") is None      # 无 M 标记 → 拒绝（与旧口径一致）


def test_filter_exrights_adjusted_and_dedup():
    """§20 P3-7：剔除调整合约（简称尾缀 A）+ 同 (cp, expiry, strike) 去重。"""
    import pandas as pd
    from vol.contracts import filter_exrights
    df = pd.DataFrame([
        {"code": "10005163", "sym": "50ETF购12月2700", "cp": "C", "expiry": "202612", "strike": 2.7, "iv": 0.20},
        {"code": "10005163A", "sym": "50ETF购12月2700A", "cp": "C", "expiry": "202612", "strike": 2.7, "iv": 0.19},
        {"code": "10005164", "sym": "50ETF沽12月2700", "cp": "P", "expiry": "202612", "strike": 2.7, "iv": 0.22},
        {"code": "10005170", "sym": "50ETF购12月2750", "cp": "C", "expiry": "202612", "strike": 2.75, "iv": 0.21},
        {"code": "10005999", "sym": "50ETF购12月2700", "cp": "C", "expiry": "202612", "strike": 2.7, "iv": 0.18},  # 重复
    ])
    out, stats = filter_exrights(df)
    assert stats == {"total": 5, "adjusted_dropped": 1, "dedup_dropped": 1}
    assert len(out) == 3
    assert "10005163A" not in set(out["code"])          # 调整合约被剔除
    assert set(out["iv"]) == {0.20, 0.22, 0.21}         # 同组合保留首条（原合约）


def test_filter_exrights_no_sym_column():
    """无简称列时仅做组合去重（口径降级不报错）。"""
    import pandas as pd
    from vol.contracts import filter_exrights
    df = pd.DataFrame([{"cp": "C", "expiry": "202612", "strike": 2.7, "iv": 0.20},
                       {"cp": "C", "expiry": "202612", "strike": 2.7, "iv": 0.19}])
    out, stats = filter_exrights(df)
    assert stats["adjusted_dropped"] == 0 and stats["dedup_dropped"] == 1 and len(out) == 1
