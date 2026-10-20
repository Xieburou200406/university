# -*- coding: utf-8 -*-
"""配置 —— 全库魔法数字唯一来源（AGENTS.md §8 M5）。代码里不再出现裸参数。"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    # 信号
    window: int = 120                # IV 分位窗口（铁律 12：正式口径唯一值）
    confirm_days: int = 2            # 状态翻转确认天数
    k_nearest: int = 20              # 类比引擎邻居数
    horizon: int = 10                # 类比预测期限（天）
    # 风控
    vega_limit: float = 881.0
    delta_limit: float = 15000.0
    liq_min_score: int = 40
    # 风控（M7-⑥ 7 条完整化：设计 §risk/rules.py 剩余 5 条的阈值）
    single_strike_limit: int = 20      # 同一行权价合计张数上限（SingleStrikeRule）
    deep_money_low: float = 0.15      # 单张 |Δ| < 0.15 = 深虚，禁加仓（DeepMoneyRule）
    deep_money_high: float = 0.85     # 单张 |Δ| > 0.85 = 深实，禁加仓（DeepMoneyRule）
    capital_base: float = 100000.0    # 账本基准资金（元，CapitalUsageRule 分母）
    capital_usage_limit: float = 0.8  # 保证金占用 / 基准资金 上限（CapitalUsageRule）
    # 成本
    fee_rate: float = 2e-4
    # 数据
    cache_version: str = "v2"        # 改解析逻辑必换（铁律 9）
    cache_days: int = 520
    # 其他
    r_free: float = 0.02

    @staticmethod
    def load(path: str | None = None) -> "Settings":
        """从 config.yaml 读取覆盖项；缺省用内置默认。yaml 不可用时静默降级。"""
        if not path:
            return Settings()
        try:
            import yaml
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            known = {f for f in Settings.__dataclass_fields__}
            return Settings(**{k: v for k, v in data.items() if k in known})
        except Exception:
            return Settings()
