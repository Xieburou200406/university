# -*- coding: utf-8 -*-
"""紧凑逻辑回归：IRLS（牛顿法）+ L2 正则 + 特征标准化，纯 numpy 无外部依赖。
选型理由（§13.3）：样本 500+ 日下低方差、系数可读、确定性可复现；LightGBM 仅作影子对照不默认上线。"""
import numpy as np


class LogisticIRLS:
    """L2 正则逻辑回归。fit(X, y) → predict_proba(X)。X 要求为有限值（NaN 由上层处理）。"""

    def __init__(self, l2: float = 1.0, max_iter: int = 50, tol: float = 1e-8):
        self.l2 = l2
        self.max_iter = max_iter
        self.tol = tol
        self.w_ = None
        self.mu_ = None          # 标准化参数
        self.sd_ = None

    def fit(self, X: np.ndarray, y: np.ndarray) -> "LogisticIRLS":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        if X.ndim != 2 or len(X) < 10 or len(np.unique(y)) < 2:
            raise ValueError("样本不足或标签单一，fail-closed 拒绝训练")
        self.mu_ = X.mean(axis=0)
        self.sd_ = X.std(axis=0) + 1e-9
        Z = np.hstack([np.ones((len(X), 1)), (X - self.mu_) / self.sd_])
        w = np.zeros(Z.shape[1])
        l2_vec = np.r_[0.0, np.full(Z.shape[1] - 1, self.l2)]   # 截距不正则
        for _ in range(self.max_iter):
            p = 1.0 / (1.0 + np.exp(-np.clip(Z @ w, -30, 30)))
            grad = Z.T @ (p - y) + l2_vec * w
            S = np.clip(p * (1 - p), 1e-6, None)
            H = (Z * S[:, None]).T @ Z + np.diag(l2_vec)
            step = np.linalg.solve(H + 1e-9 * np.eye(len(w)), grad)
            w = w - step
            if np.max(np.abs(step)) < self.tol:
                break
        self.w_ = w
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        Z = np.hstack([np.ones((len(X), 1)), (X - self.mu_) / self.sd_])
        return 1.0 / (1.0 + np.exp(-np.clip(Z @ self.w_, -30, 30)))

    @property
    def coef_(self) -> np.ndarray:
        """标准化后的特征系数（可读性：回答'为什么看涨'）。"""
        return (self.w_[1:] / self.sd_) if self.w_ is not None else None


def platt_calibrate(scores: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """Platt 缩放：p = sigmoid(a + b·score)。小样本下等渗易过拟合，用 Platt。"""
    scores = np.asarray(scores, dtype=float)
    y = np.asarray(y, dtype=float)
    a, b = 0.0, 1.0
    for _ in range(100):
        p = 1.0 / (1.0 + np.exp(-(a + b * np.clip(scores, -10, 10))))
        g_a = (p - y).sum()
        g_b = ((p - y) * scores).sum()
        h_aa = max((p * (1 - p)).sum(), 1e-6)
        h_ab = (p * (1 - p) * scores).sum()
        h_bb = max((p * (1 - p) * scores * scores).sum(), 1e-6)
        det = h_aa * h_bb - h_ab * h_ab
        if abs(det) < 1e-12:
            break
        a -= (h_bb * g_a - h_ab * g_b) / det
        b -= (h_aa * g_b - h_ab * g_a) / det
        b = float(np.clip(b, 1e-4, 100.0))                    # 防塌缩成硬标签
    return float(a), float(b)
