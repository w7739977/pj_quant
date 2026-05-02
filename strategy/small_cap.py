"""
小市值多因子选股策略

核心逻辑:
1. 从全A股筛选市值 5~50 亿的小盘股
2. 用多因子打分（动量、波动率、换手率、技术指标、基本面）
3. 买入综合得分最高的 N 只股票
4. 月度调仓，个股止损 -10%
"""

import pandas as pd
import numpy as np
from strategy.base import BaseStrategy
from factors.calculator import compute_stock_pool_factors
from config.settings import INITIAL_CAPITAL


class SmallCapStrategy(BaseStrategy):
    """小市值多因子选股策略"""

    def __init__(
        self,
        min_cap: float = 5e8,
        max_cap: float = 5e9,
        top_n: int = 10,
        stop_loss: float = -0.10,
    ):
        self.min_cap = min_cap
        self.max_cap = max_cap
        self.top_n = top_n
        self.stop_loss = stop_loss

    @property
    def name(self) -> str:
        return f"small_cap_top{self.top_n}"

    def _score_stocks(self, factor_df: pd.DataFrame) -> pd.DataFrame:
        """
        多因子打分

        对每个因子做截面排名（分位数），然后加权求和得到综合得分。
        """
        if factor_df.empty:
            return factor_df

        df = factor_df.copy()

        # 定义因子方向: 正=越大越好, 负=越小越好
        # 与 ml/ranker.py FEATURE_COLS 对齐（24 因子）
        factor_direction = {
            # 动量
            "mom_5d": 1,
            "mom_10d": 1,
            "mom_20d": 1,
            "mom_60d": 1,
            # 波动率：低波动好
            "vol_10d": -1,
            "vol_20d": -1,
            # 换手率：适度活跃好
            "avg_turnover_5d": 1,
            "avg_turnover_20d": 1,
            "turnover_accel": 1,
            "turnover_rate": 1,
            # 量价
            "vol_price_diverge": 1,
            "volume_surge": 1,
            "volume_ratio": 1,
            # MA 偏离：低偏离好（超跌反弹）
            "ma5_bias": -1,
            "ma10_bias": -1,
            "ma20_bias": -1,
            # 技术
            "rsi_14": -1,         # RSI：超卖好
            # 估值
            "pe_ttm": -1,
            "pb": -1,
            # 情绪因子 sentiment_score 暂禁用（sentiment_history 表未回填）
            # P0 财务因子
            "roe_yearly": 1,        # ROE 越高越好
            "or_yoy": 1,            # 营收增速越高越好
            "dt_eps_yoy": 1,        # EPS 增速越高越好
            "debt_to_assets": -1,   # 负债率越低越好
        }

        scores = pd.Series(0.0, index=df.index)

        for factor_name, direction in factor_direction.items():
            if factor_name not in df.columns:
                continue
            series = pd.to_numeric(df[factor_name], errors="coerce")
            valid = series.notna()
            if valid.sum() < 5:
                continue

            # 截面排名 → 分位数 [0, 1]
            rank = series.rank(pct=True, na_option="keep")

            # 根据方向调整: direction=-1 时反转
            if direction == -1:
                rank = 1 - rank

            # 权重: 财务核心因子稍加权，其余等权
            if factor_name in ("roe_yearly", "or_yoy", "dt_eps_yoy"):
                weight = 1.5
            else:
                weight = 1.0

            scores += rank.fillna(0.5) * weight

        df["score"] = scores
        return df

    def generate_signals(self, factor_df: pd.DataFrame = None, **kwargs) -> pd.DataFrame:
        """
        生成选股信号

        Parameters
        ----------
        factor_df : DataFrame  因子矩阵，如为 None 则自动计算

        Returns
        -------
        DataFrame: [code, score, action]
        """
        if factor_df is None:
            factor_df = compute_stock_pool_factors(self.min_cap, self.max_cap)

        if factor_df.empty:
            return pd.DataFrame()

        scored = self._score_stocks(factor_df)
        scored = scored.sort_values("score", ascending=False)

        # 选 top N
        top = scored.head(self.top_n)
        signals = top[["code", "score"]].copy()
        signals["action"] = "buy"
        signals = signals.rename(columns={"score": "momentum"})
        return signals.reset_index(drop=True)

    def get_portfolio_recommendation(self) -> dict:
        """
        生成完整的持仓建议

        Returns
        -------
        dict: {
            "stocks": [{code, name, score, suggested_weight}],
            "total_stocks": int,
            "strategy": str,
        }
        """
        signals = self.generate_signals()
        if signals.empty:
            return {"stocks": [], "total_stocks": 0, "strategy": self.name}

        # 等权配置
        per_stock = INITIAL_CAPITAL / self.top_n

        stocks = []
        for _, row in signals.iterrows():
            stocks.append({
                "code": row["code"],
                "score": round(row["momentum"], 3),
                "suggested_amount": round(per_stock, 0),
            })

        return {
            "stocks": stocks,
            "total_stocks": len(stocks),
            "strategy": self.name,
            "per_stock_capital": round(per_stock, 0),
        }
