# -*- coding: utf-8 -*-
"""合约代码解析 —— AGENTS.md 铁律 1/4 的唯一入口。
示例: 510050C2610M02700 → 510050, Call, 2026-10 到期(第4个周三), 行权价 2.700 元(÷1000!)。"""
import re
from dataclasses import dataclass
from datetime import date

CODE_RE = re.compile(r"^(\d{6})([CP])(\d{2})(\d{2})M?(\d{4,5})[A-Z]?$")


@dataclass(frozen=True)
class ContractSpec:
    underlying: str
    cp: str            # "C" / "P"
    expiry: date       # 到期月第 4 个周三（上交所 ETF 期权月合约）
    strike: float      # 单位: 元（已除 1000 的正确量纲）

    def moneyness(self, spot: float) -> float:
        return self.strike / spot - 1.0

    def tier_label(self, spot: float, atol: float = 0.02) -> str:
        m = self.moneyness(spot)
        if abs(m) < atol:
            return "ATM"
        side = "虚值" if (m > 0) == (self.cp == "C") else "实值"
        return f"{side}{abs(round(m * 100))}%"


def parse_contract_code(code: str) -> ContractSpec:
    """解析合约代码。行权价 = 末 4 位 ÷ 1000（铁律 1，错成 /10000 会选错 ATM 且难察觉）。"""
    m = CODE_RE.match(code)
    if not m:
        raise ValueError(f"无法解析合约代码: {code!r}")
    ul, cp, yy, mm, strike4 = m.groups()
    return ContractSpec(
        underlying=ul,
        cp=cp,
        expiry=expiry_from_code(f"20{yy}{mm}"),
        strike=int(strike4) / 1000.0,
    )


def expiry_from_code(ym: str) -> date:
    """'202610' → 该月第 4 个周三（铁律 4：不依赖今日合约字典，历史退市合约也能算）。
    实现单一来源：vol.calendar.exchange_rules.fourth_wednesday（§18 规则库）。"""
    from vol.calendar.exchange_rules import fourth_wednesday
    return fourth_wednesday(int(ym[:4]), int(ym[4:6]))


def expiry_from_code_str(code: str) -> date:
    return parse_contract_code(code).expiry


def parse_sse_etf_code(code: str, underlying: str = "510050"):
    """数据层严格解析（与 demo 期 SYMBOL_RE 行为逐位一致）：必须含 M 标记，
    不匹配返回 None。注意不加 $ 尾锚——真实代码含除权调整后缀（如尾缀 A），
    旧解析按前缀匹配放行，这里必须保持一致，否则当日截面会整表解析失败。"""
    m = re.match(rf"^{underlying}([CP])(\d{{4}})M(\d{{5}})", code)
    if not m:
        return None
    return {"cp": m.group(1), "expiry": "20" + m.group(2), "strike": int(m.group(3)) / 1000.0}
