"""
持仓跟踪模块
"""

import json
import os
from datetime import datetime
from data.storage import load_portfolio, save_portfolio
from config.settings import INITIAL_CAPITAL


class PortfolioTracker:
    """持仓跟踪器"""

    def __init__(self):
        self.state = load_portfolio()

    @property
    def cash(self) -> float:
        return self.state.get("cash", INITIAL_CAPITAL)

    @property
    def holdings(self) -> dict:
        return self.state.get("holdings", {})

    def update_after_buy(self, symbol: str, shares: int, price: float, cost: float):
        """买入后更新持仓"""
        self.state["cash"] -= (price * shares + cost)
        self.state["holdings"][symbol] = {
            "shares": shares,
            "avg_cost": price,
            "buy_date": datetime.now().strftime("%Y-%m-%d"),
        }
        save_portfolio(self.state)

    def update_after_sell(self, symbol: str, price: float, cost: float):
        """卖出后更新持仓"""
        if symbol in self.state["holdings"]:
            shares = self.state["holdings"][symbol]["shares"]
            self.state["cash"] += (price * shares - cost)
            del self.state["holdings"][symbol]
        save_portfolio(self.state)

    def get_summary(self, current_prices: dict = None) -> str:
        """生成持仓摘要"""
        lines = [
            f"{'='*40}",
            f"持仓摘要 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
            f"{'='*40}",
            f"可用资金: {self.cash:,.2f} 元",
            f"持仓明细:",
        ]

        total_value = self.cash
        for symbol, info in self.holdings.items():
            shares = info["shares"]
            avg_cost = info["avg_cost"]
            current_price = (current_prices or {}).get(symbol, avg_cost)
            market_value = current_price * shares
            pnl = (current_price - avg_cost) * shares
            pnl_pct = (current_price / avg_cost - 1) * 100 if avg_cost > 0 else 0

            lines.append(
                f"  {symbol}: {shares}股 @ {avg_cost:.3f}"
                f" -> 现价 {current_price:.3f}"
                f" | 市值 {market_value:,.0f}"
                f" | 盈亏 {pnl:+,.0f} ({pnl_pct:+.1f}%)"
            )
            total_value += market_value

        total_pnl = total_value - INITIAL_CAPITAL
        total_pnl_pct = total_pnl / INITIAL_CAPITAL * 100
        lines.extend([
            f"{'─'*40}",
            f"总资产: {total_value:,.2f} 元",
            f"总盈亏: {total_pnl:+,.2f} ({total_pnl_pct:+.2f}%)",
            f"{'='*40}",
        ])
        return "\n".join(lines)
