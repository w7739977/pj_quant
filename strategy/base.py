"""
策略基类
"""

from abc import ABC, abstractmethod
import pandas as pd


class BaseStrategy(ABC):
    """所有策略的基类"""

    @abstractmethod
    def generate_signals(self, price_data: dict[str, pd.DataFrame]) -> pd.DataFrame:
        """
        生成交易信号

        Parameters
        ----------
        price_data : dict  {symbol: DataFrame}

        Returns
        -------
        DataFrame with columns: [date, symbol, action]
            action: "buy", "sell", or "hold"
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        pass
