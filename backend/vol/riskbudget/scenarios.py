# -*- coding: utf-8 -*-
"""情景集构建（§14.1）：复用类比引擎的特征空间做 k 近邻检索，
取每个邻居未来 horizon 日的 (ΔIV, Δspot) 作为一条情景——不引入新模型。"""
import numpy as np
import pandas as pd

FEATS = ["iv_pct", "vrp", "mom20", "ts_slope", "skew25"]


def build_features(df: pd.DataFrame) -> list[dict]:
    """逐日特征（与 m3 analog_states 同口径）。df 列：date, spot, iv_near, iv_next, skew25, rvol20。"""
    feats = []
    n = len(df)
    for i in range(n):
        lo = max(0, i - 120)
        hist = df["iv_near"][lo:i + 1]
        pct = float((hist <= df["iv_near"][i]).mean())
        rv = df["rvol20"][i] if not np.isnan(df["rvol20"][i]) else df["iv_near"][i]
        mom = df["spot"][i] / df["spot"][i - 20] - 1.0 if i >= 20 else 0.0
        nxt = df["iv_next"][i] if df["iv_next"][i] else df["iv_near"][i]
        feats.append({"date": df["date"][i], "iv_pct": pct,
                      "vrp": df["iv_near"][i] - rv, "mom20": mom,
                      "ts_slope": df["iv_near"][i] - nxt,
                      "skew25": df["skew25"][i] if df["skew25"][i] is not None else 0.0})
    return feats


def build_scenarios(df: pd.DataFrame, k: int = 20, horizon: int = 10,
                    window: int = 120) -> dict:
    """在末日 T 找 k 个最相似历史日，取其未来 horizon 日变化作情景。
    返回 {scenarios: [{delta_iv, delta_spot, neighbor_date}], mu_iv, sigma_iv, mu_spot, sigma_spot,
          degraded, reason}。样本不足 → degraded=True（§16.4 L2 降级）。"""
    n = len(df)
    if n < window + horizon + 10:
        return {"scenarios": [], "degraded": True,
                "reason": f"面板 {n} 日不足（需 ≥{window + horizon + 10}），降级为零变动情景"}
    feats = build_features(df)
    i = n - 1
    arr = np.array([[f[kk] for kk in FEATS] for f in feats[:i]])
    mu, sd = arr.mean(axis=0), arr.std(axis=0) + 1e-9
    zx = np.array([feats[i][kk] for kk in FEATS])
    dist = np.sqrt((((arr - mu) / sd - (zx - mu) / sd) ** 2).sum(axis=1))
    idxs = [int(j) for j in np.argsort(dist) if j < i - horizon][:k]
    if len(idxs) < max(5, k // 2):
        return {"scenarios": [], "degraded": True,
                "reason": f"有效邻居仅 {len(idxs)} 个，降级为零变动情景"}
    scenarios = []
    for j in idxs:
        d_iv = float(df["iv_near"][j + horizon] - df["iv_near"][j])
        d_spot = float(df["spot"][j + horizon] / df["spot"][j] - 1.0)
        scenarios.append({"delta_iv": d_iv, "delta_spot": d_spot, "neighbor_date": str(df["date"][j])})
    d_ivs = np.array([s["delta_iv"] for s in scenarios])
    d_sps = np.array([s["delta_spot"] for s in scenarios])
    return {"scenarios": scenarios, "degraded": False, "reason": "OK",
            "mu_iv": float(d_ivs.mean()), "sigma_iv": float(d_ivs.std()),
            "mu_spot": float(d_sps.mean()), "sigma_spot": float(d_sps.std())}
