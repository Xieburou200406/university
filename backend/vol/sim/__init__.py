# -*- coding: utf-8 -*-
"""模拟盘核心（§12.2，M6-① 单机日频诚实版）。

与规格的取舍（写进 §12.5 诚实声明的口径）：
- 回放步长 = 1 交易日（EOD 收盘截面）：真实日内 tick 不可得，不做合成分钟路径；
- 撮合不做订单簿：市价单 = 收盘 BS 价 ± 半价差吃单；限价单触及即成（按当日收盘截面判定）；
- SSE/交互式 stepping 随前端页下轮接入，本轮为"批量预编程订单 + 一次性回放"。
"""
from vol.sim.account import SimAccount, SimAccountService
from vol.sim.broker import (HALF_SPREAD, FEE_PER_UNIT, R_FREE, SimLeg, SimOrder,
                            option_px, unit_cost)
from vol.sim.clock import ReplayClock
from vol.sim.replay import ReplayResult, ReplaySession
from vol.sim.templates import REGISTRY, round_strike

__all__ = ["SimAccount", "SimAccountService", "ReplayClock", "ReplaySession",
           "ReplayResult", "SimLeg", "SimOrder", "option_px", "unit_cost",
           "HALF_SPREAD", "FEE_PER_UNIT", "R_FREE", "REGISTRY", "round_strike"]
