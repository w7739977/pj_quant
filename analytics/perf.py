"""
统一绩效指标计算

backtest 和 simulation 共用，确保 Sharpe 等指标口径一致。
"""

import numpy as np


def sharpe_ratio(daily_returns: np.ndarray, risk_free: float = 0.02) -> float:
    """
    日频收益序列 → 年化夏普比率

    Parameters
    ----------
    daily_returns : array-like  每日收益率序列
    risk_free : float  年化无风险利率，默认 2%
    """
    daily_returns = np.asarray(daily_returns, dtype=float)
    if len(daily_returns) < 2 or np.std(daily_returns) == 0:
        return 0.0
    excess = daily_returns - risk_free / 252
    return float(excess.mean() / np.std(daily_returns) * np.sqrt(252))
