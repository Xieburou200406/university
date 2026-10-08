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
