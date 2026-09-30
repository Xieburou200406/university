# -*- coding: utf-8 -*-
"""
signal.py — L3 信号层（demo 版）
引擎 A：IV 滚动分位数（均值回归，120 日窗口 + 连续2日确认状态机）
引擎 B：历史情景类比（k-NN，简化 4 维特征 + 5 策略候选池）
两个引擎实现同一接口 run(df) -> dict，可插拔。
"""
import math

import numpy as np

WINDOW = 120
K_NEAREST = 20


# ---------- 引擎 A：分位数 + 状态机 ----------

class PercentileEngine:
    def __init__(self, window=WINDOW):
        self.window = window
        self.state = "NEUTRAL"
        self._streak = 0  # 连续处于超买/超卖区的天数

    def run(self, atm_iv_series):
        """atm_iv_series: list[float]（按日期升序，最后一项是今天）"""
        s = [v for v in atm_iv_series if v is not None]
        if len(s) < self.window:
            return {"engine": "percentile", "signal": "INSUFFICIENT_HISTORY",
                    "detail": f"历史不足 {self.window} 日，当前 {len(s)} 日"}
        hist = s[-(self.window + 1):-1]  # 窗口用截至昨日的数据，防止自包含
        today = s[-1]
        pct = sum(1 for v in hist if v <= today) / len(hist)

        zone = "HIGH" if pct >= 0.80 else ("LOW" if pct <= 0.20 else "MID")
        if zone != "MID" and zone == getattr(self, "_last_zone", None):
            self._streak += 1
        elif zone != "MID":
            self._streak = 1
        else:
            self._streak = 0
        self._last_zone = zone

        # 连续 2 日处于同一区间才翻转状态
        if self._streak >= 2:
            self.state = "SHORT_VOL" if zone == "HIGH" else "LONG_VOL"
        elif zone == "MID":
            self.state = "NEUTRAL"

        return {"engine": "percentile", "signal": self.state, "iv_pct": round(pct, 3),
                "iv_now": round(today, 4), "window": self.window,
                "streak": self._streak, "confirm_needed": max(0, 2 - self._streak)}


# ---------- 引擎 B：历史情景类比 ----------

FEATURES = ["iv_pct", "vrp", "mom20", "ts_slope"]


def build_features(dates, atm_iv, rvol20, next_month_iv, spot):
    """返回 feature dict 列表（逐日）。VRP=IV-已实现波动率；ts_slope=近月IV-次月IV。"""
    feats = []
    for i in range(len(dates)):
        lo = max(0, i - WINDOW)
        hist = [v for v in atm_iv[lo:i + 1] if v is not None]
        iv = atm_iv[i]
        pct = (sum(1 for v in hist if v <= iv) / len(hist)) if hist else 0.5
        rv = rvol20[i] if rvol20[i] is not None else iv
        mom = (spot[i] / spot[i - 20] - 1.0) if i >= 20 and spot[i - 20] else 0.0
        slope = (iv - next_month_iv[i]) if next_month_iv[i] else 0.0
        feats.append({"date": dates[i], "iv_pct": pct, "vrp": iv - rv,
                      "mom20": mom, "ts_slope": slope})
    return feats


class AnalogEngine:
    def __init__(self, k=K_NEAREST, horizon=10):
        self.k = k
        self.horizon = horizon

    def run(self, feats, spot, atm_iv):
        """
        feats: build_features 输出（含今天在最后一位）
        返回：最相似历史日列表 + 各策略加权统计 + 选出的策略。
        策略收益按"波动率方向敞口"近似：用 ATM IV 变化 × 期权 Vega 代理，剔除方向项。
        """
        if len(feats) < self.k + self.horizon + 10:
            return {"engine": "analog", "signal": "INSUFFICIENT_HISTORY"}
        x = feats[-1]
        arr = np.array([[f[k] for k in FEATURES] for f in feats[:-1]])
        mu, sd = arr.mean(axis=0), arr.std(axis=0) + 1e-9
        zx = np.array([x[k] for k in FEATURES])
        dists = np.sqrt((((arr - mu) / sd - (zx - mu) / sd) ** 2).sum(axis=1))

        order = np.argsort(dists)
        # 相似日：要求在 horizon 天之前，保证其向后收益可得
        idxs = [int(i) for i in order if i < len(feats) - 1 - self.horizon][:self.k]
        H = self.horizon

        # 每个相似日：之后 H 天的 IV 变化（策略收益的代理）与标的涨跌
        outcomes = []
        for i in idxs:
            w = 1.0 / (1.0 + dists[i])
            div = atm_iv[i + H] - atm_iv[i]
            ds = spot[i + H] / spot[i] - 1.0
            outcomes.append((w, div, ds))

        W = sum(w for w, _, _ in outcomes)
        # 策略池（每 1 张名义，收益代理：vega敞口×IV变化 + delta敞口×标的涨跌）
        pool = {
            "买跨式(long straddle)":      {"vega": +2, "delta": 0},
            "卖跨式(short straddle,Δ对冲)": {"vega": -2, "delta": 0},
            "买日历(calendar long)":       {"vega": +1, "delta": 0},
            "单腿买Call":                  {"vega": +1, "delta": +1},
            "观望":                        {"vega": 0, "delta": 0},
        }
        stats = {}
        for name, g in pool.items():
            rets = [w * (g["vega"] * div + g["delta"] * ds) for w, div, ds in outcomes]
            mu_r = sum(rets) / W
            wins = sum(1 for r in rets if r > 0) / len(rets)
            p10 = float(np.percentile(rets, 10))
            stats[name] = {"exp": round(mu_r, 4), "win": round(wins, 3), "p10": round(p10, 4)}

        valid = {k: v for k, v in stats.items() if k != "观望"}
        best = max(valid, key=lambda k: valid[k]["exp"])
        if valid[best]["win"] >= 0.55 and valid[best]["p10"] > -0.05:
            signal, conf = best, valid[best]["win"]
        else:
            signal, conf = "观望（无共识）", 0.0

        neighbors = [{"date": feats[i]["date"], "dist": round(float(dists[i]), 3),
                      "iv_after10d": round((atm_iv[i + H] - atm_iv[i]) * 100, 2)}
                     for i in sorted(idxs, key=lambda j: dists[j])[:8]]
        return {"engine": "analog", "signal": signal, "confidence": round(conf, 3),
                "neighbors": neighbors, "strategy_stats": stats,
                "today_feat": {k: round(x[k], 3) for k in FEATURES}}


def synthetic_history(n=420, seed=7):
    """生成 n 日合成截面：标的几何随机游走 + IV 均值回归加噪声（仅 demo/验证用，报告需标注）。"""
    rng = np.random.default_rng(seed)
    dates, spot, atm_iv, nm_iv, rvol = [], [], [], [], []
    s, iv, rv = 3.0, 0.18, 0.15
    for i in range(n):
        dates.append(f"SIM-{i:04d}")
        s *= math.exp(rng.normal(0.0004, 0.012))
        iv = max(0.09, min(0.42, iv + rng.normal(0.18 - iv, 0.01) * 0.06 + rng.normal(0, 0.006)))
        rv = 0.85 * rv + 0.15 * iv + rng.normal(0, 0.004)
        spot.append(s); atm_iv.append(iv); nm_iv.append(iv - 0.008)
        rvol.append(min(0.45, max(0.06, rv)))
    return dates, spot, atm_iv, nm_iv, rvol
