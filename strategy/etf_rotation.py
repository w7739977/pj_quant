"""
ETF 动量轮动策略

核心逻辑：
1. 每 N 个交易日评估一次（默认 20 个交易日 ≈ 1 个月）
2. 计算各 ETF 过去 N 天的动量（涨幅）
3. 如果最强 ETF 的动量 > 0，满仓持有
4. 如果最强 ETF 的动量 <= 0，持有国债 ETF（防御）
5. 如果已持有最强 ETF，则不交易
"""

import pandas as pd
import numpy as np
from strategy.base import BaseStrategy
from config.settings import ETF_POOL, MOMENTUM_LOOKBACK_DAYS, REBALANCE_DAYS


class ETFRotationStrategy(BaseStrategy):
    """ETF 动量轮动策略"""

    def __init__(
        self,
        etf_pool: dict = None,
        lookback: int = MOMENTUM_LOOKBACK_DAYS,
        rebalance_days: int = REBALANCE_DAYS,
        defense_symbol: str = "511010",  # 国债 ETF
    ):
        self.etf_pool = etf_pool or ETF_POOL
        self.lookback = lookback
        self.rebalance_days = rebalance_days
        self.defense_symbol = defense_symbol

    @property
    def name(self) -> str:
        return f"etf_rotation_{self.lookback}d"

    def _calc_momentum(self, df: pd.DataFrame, date: pd.Timestamp) -> float:
        """计算某只 ETF 在 date 时的动量（过去 lookback 天涨幅）"""
        hist = df[df["date"] <= date].tail(self.lookback + 1)
        if len(hist) < 2:
            return 0.0
        return (hist["close"].iloc[-1] / hist["close"].iloc[0] - 1.0)

    def generate_signals(self, price_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """
        生成 ETF 轮动交易信号

        Returns
        -------
        DataFrame: [date, symbol, action, momentum]
        """
        # 获取所有交易日的并集
        all_dates = set()
        for df in price_data.values():
            all_dates.update(df["date"].tolist())
        all_dates = sorted(all_dates)

        signals = []
        last_rebalance_idx = -self.rebalance_days  # 确保第一个交易日就评估
        current_holding = None

        for i, date in enumerate(all_dates):
            if i - last_rebalance_idx < self.rebalance_days:
                continue  # 非调仓日

            last_rebalance_idx = i

            # 计算所有 ETF 的动量
            momentums = {}
            for symbol, df in price_data.items():
                if symbol == self.defense_symbol:
                    continue  # 防御资产不参与动量排名
                m = self._calc_momentum(df, pd.Timestamp(date))
                if not np.isnan(m):
                    momentums[symbol] = m

            if not momentums:
                continue

            # 选择动量最强的 ETF
            best_symbol = max(momentums, key=momentums.get)
            best_momentum = momentums[best_symbol]

            # 如果最强动量 <= 0，切换到防御资产
            target = best_symbol if best_momentum > 0 else self.defense_symbol

            # 生成信号
            if current_holding and current_holding != target:
                # 卖出旧持仓
                signals.append({
                    "date": date,
                    "symbol": current_holding,
                    "action": "sell",
                    "momentum": momentums.get(current_holding, 0),
                })

            if current_holding != target:
                # 买入新标的
                signals.append({
                    "date": date,
                    "symbol": target,
                    "action": "buy",
                    "momentum": momentums.get(target, 0) if target != self.defense_symbol else 0,
                })
                current_holding = target

        return pd.DataFrame(signals)
