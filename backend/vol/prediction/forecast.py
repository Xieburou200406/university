# -*- coding: utf-8 -*-
"""特征/标签构建 + 弃权规则（§13.2/§13.4）。
输入面板 df 列：date, spot, open（次日开盘标签用）, atm_iv, iv_pct_120, skew25,
term_slope, vrp, momentum_20d。全部 T 日收盘可得（铁律 5：特征不含任何 T+1 信息）。"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

FEATURES = ["atm_iv", "iv_pct_120", "skew25", "term_slope", "vrp", "momentum_20d"]
FLAT_BAND = 0.0005          # |隔夜跳空| < 5bp 记 FLAT，不参与训练/评估
ABSTAIN_LO, ABSTAIN_HI = 0.45, 0.55   # P(up) 落在死区 → 弃权（§13.3）


@dataclass
class ForecastResult:
    date: str
    p_up_raw: float | None
    p_up_cal: float | None
    abstain: bool
    reason: str
    coef: dict = field(default_factory=dict)


def build_dataset(df: pd.DataFrame) -> pd.DataFrame:
    """逐日特征矩阵 + 次日开盘方向标签（FLAT→NaN 排除）。"""
    d = df.sort_values("date").reset_index(drop=True)
    gap = d["open"].shift(-1) / d["spot"] - 1.0        # 隔夜跳空（次日开盘/当日收盘）
    d["y"] = np.where(gap > FLAT_BAND, 1.0, np.where(gap < -FLAT_BAND, 0.0, np.nan))
    d["next_date"] = d["date"].shift(-1)
    return d


class OpenDirectionForecaster:
    """训练（截至 T 日）→ 预测 T+1 开盘方向。全程 fail-closed。"""

    def __init__(self, l2: float = 1.0):
        self.l2 = l2
        self.model = None
        self.a_, self.b_ = 0.0, 1.0                     # Platt 参数

    def fit(self, df: pd.DataFrame) -> "OpenDirectionForecaster":
        """用历史完整样本训练 + 校准。样本 <60 或特征缺失率过高 → 抛 ValueError（上层弃权）。"""
        from vol.prediction.model import LogisticIRLS, platt_calibrate
        data = build_dataset(df).dropna(subset=["y"] + FEATURES)
        if len(data) < 60:
            raise ValueError(f"有效样本仅 {len(data)} 日（需 ≥60），fail-closed 弃权")
        X = data[FEATURES].to_numpy(float)
        y = data["y"].to_numpy(float)
        self.model = LogisticIRLS(l2=self.l2).fit(X, y)
        scores = self.model.predict_proba(X)
        self.a_, self.b_ = platt_calibrate(scores, y)    # 训练集内校准（v1 简化；上线换 out-of-fold）
        return self

    def forecast(self, df: pd.DataFrame) -> ForecastResult:
        """T 日收盘特征 → 次日开盘方向概率。弃权规则：样本不足 / 特征缺失 / P(up) 死区。"""
        d = df.sort_values("date").reset_index(drop=True)
        last = d.iloc[-1]
        missing = [f for f in FEATURES if pd.isna(last.get(f))]
        if missing:
            return ForecastResult(str(last["date"]), None, None, True, f"特征缺失: {missing}")
        if self.model is None:
            try:
                self.fit(d)
            except ValueError as e:
                return ForecastResult(str(last["date"]), None, None, True, str(e))
        x = last[FEATURES].to_numpy(float).reshape(1, -1)
        p_raw = float(self.model.predict_proba(x)[0])
        p_cal = 1.0 / (1.0 + np.exp(-(self.a_ + self.b_ * np.clip(p_raw, -10, 10))))
        coef = {f: round(float(c), 4) for f, c in zip(FEATURES, self.model.coef_)}
        if ABSTAIN_LO <= p_cal <= ABSTAIN_HI:
            return ForecastResult(str(last["date"]), round(p_raw, 4), round(float(p_cal), 4),
                                  True, f"P(up)={p_cal:.3f} 落在弃权死区 [{ABSTAIN_LO},{ABSTAIN_HI}]",
                                  coef)
        return ForecastResult(str(last["date"]), round(p_raw, 4), round(float(p_cal), 4),
                              False, "OK", coef)


# ---------- 评估（§13.3）：walk-forward，绝不重训进未来 ----------

def _brier(probs: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((probs - y) ** 2))


def _auc(probs: np.ndarray, y: np.ndarray) -> float:
    order = np.argsort(probs)
    ranks = np.empty(len(probs))
    ranks[order] = np.arange(1, len(probs) + 1)
    # 并列取平均秩
    for v in np.unique(probs):
        mask = probs == v
        if mask.sum() > 1:
            ranks[mask] = ranks[mask].mean()
    n1, n0 = y.sum(), (1 - y).sum()
    if n1 == 0 or n0 == 0:
        return float("nan")
    return float((ranks[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def walk_forward_evaluate(df: pd.DataFrame, min_train: int = 120,
                          step: int = 1) -> dict:
    """滚动训练评估：第 i 日只用 <i 的数据训练。返回 AUC/Brier/方向准确率 + 双基准对比。"""
    data = build_dataset(df).dropna(subset=["y"] + FEATURES).reset_index(drop=True)
    n = len(data)
    if n < min_train + 30:
        return {"ok": False, "reason": f"有效样本 {n} 不足以 walk-forward（需 ≥{min_train + 30}）"}
    probs, ys, preds, prev_dir = [], [], [], []
    for i in range(min_train, n, step):
        f = OpenDirectionForecaster().fit(data.iloc[:i])
        x = data.iloc[i][FEATURES].to_numpy(float).reshape(1, -1)
        p = float(f.model.predict_proba(x)[0])
        p = 1.0 / (1.0 + np.exp(-(f.a_ + f.b_ * p)))
        probs.append(p)
        ys.append(data.iloc[i]["y"])
        preds.append(1 if p > 0.5 else 0)
        # 基准1：延续昨日方向（昨日 y，FLAT 记 0.5 弃权不计分）
        prev_dir.append(data.iloc[i - 1]["y"])
    probs, ys = np.array(probs), np.array(ys)
    preds = np.array(preds)
    prev_dir = np.array(prev_dir)
    dir_acc = float((preds == ys).mean())
    # 基准：延续昨日方向
    bm_mask = ~np.isnan(prev_dir)
    bm_acc = float((prev_dir[bm_mask] == ys[bm_mask]).mean()) if bm_mask.any() else float("nan")
    return {"ok": True, "n_eval": int(len(ys)), "auc": round(_auc(probs, ys), 4),
            "brier": round(_brier(probs, ys), 4),
            "dir_acc": round(dir_acc, 4), "benchmark_persist_acc": round(bm_acc, 4),
            "benchmark_coin_acc": 0.5}
