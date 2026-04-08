"""
测试 portfolio/tracker.py 中的持仓跟踪
"""

import sys
import os
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from config.settings import INITIAL_CAPITAL

# 默认持仓状态（mock load_portfolio 返回值）
_DEFAULT_STATE = {"cash": INITIAL_CAPITAL, "holdings": {}, "total_value": INITIAL_CAPITAL}


def _fresh_state():
    """每次测试返回全新的初始状态"""
    return {
        "cash": INITIAL_CAPITAL,
        "holdings": {},
        "total_value": INITIAL_CAPITAL,
    }


class TestBuyAndSell:
    """买入 → 卖出基本流程"""

    @patch("portfolio.tracker.save_portfolio")
    @patch("portfolio.tracker.load_portfolio")
    def test_buy_decreases_cash_adds_holding(self, mock_load, mock_save):
        mock_load.return_value = _fresh_state()
        from portfolio.tracker import PortfolioTracker
        tracker = PortfolioTracker()

        initial_cash = tracker.cash
        tracker.update_after_buy("000001", 100, 10.0, 5.0)

        # 现金减少: 股价×数量 + 手续费
        expected_cash = initial_cash - (10.0 * 100 + 5.0)
        assert tracker.cash == expected_cash
        assert "000001" in tracker.holdings
        assert tracker.holdings["000001"]["shares"] == 100
        assert tracker.holdings["000001"]["avg_cost"] == 10.0
        mock_save.assert_called()

    @patch("portfolio.tracker.save_portfolio")
    @patch("portfolio.tracker.load_portfolio")
    def test_sell_increases_cash_removes_holding(self, mock_load, mock_save):
        state = _fresh_state()
        state["cash"] = INITIAL_CAPITAL - 1005.0  # 已买入花费
        state["holdings"]["000001"] = {
            "shares": 100, "avg_cost": 10.0, "buy_date": "2026-04-01"
        }
        mock_load.return_value = state
        from portfolio.tracker import PortfolioTracker
        tracker = PortfolioTracker()

        result = tracker.update_after_sell("000001", 12.0, 5.0)

        assert result is True
        assert "000001" not in tracker.holdings
        # 现金增加: 卖价×数量 - 手续费
        expected_cash = (INITIAL_CAPITAL - 1005.0) + (12.0 * 100 - 5.0)
        assert tracker.cash == expected_cash
        mock_save.assert_called()


class TestPyramiding:
    """加仓（同一只股票多次买入）"""

    @patch("portfolio.tracker.save_portfolio")
    @patch("portfolio.tracker.load_portfolio")
    def test_pyramiding_correct_avg(self, mock_load, mock_save):
        mock_load.return_value = _fresh_state()
        from portfolio.tracker import PortfolioTracker
        tracker = PortfolioTracker()

        # 第一次: 100股@10元
        tracker.update_after_buy("000001", 100, 10.0, 0.0)
        # 第二次: 100股@12元
        tracker.update_after_buy("000001", 100, 12.0, 0.0)

        holding = tracker.holdings["000001"]
        assert holding["shares"] == 200
        # 均价: (100*10 + 100*12) / 200 = 11.0
        assert holding["avg_cost"] == 11.0

    @patch("portfolio.tracker.save_portfolio")
    @patch("portfolio.tracker.load_portfolio")
    def test_pyramiding_preserves_buy_date(self, mock_load, mock_save):
        mock_load.return_value = _fresh_state()
        from portfolio.tracker import PortfolioTracker
        tracker = PortfolioTracker()

        tracker.update_after_buy("000001", 100, 10.0, 0.0)
        first_date = tracker.holdings["000001"]["buy_date"]

        tracker.update_after_buy("000001", 100, 12.0, 0.0)
        # 加仓后 buy_date 应保持首次买入日期
        assert tracker.holdings["000001"]["buy_date"] == first_date


class TestSellNonexistent:
    """卖出不存在的股票"""

    @patch("portfolio.tracker.save_portfolio")
    @patch("portfolio.tracker.load_portfolio")
    def test_sell_nonexistent_returns_false(self, mock_load, mock_save):
        mock_load.return_value = _fresh_state()
        from portfolio.tracker import PortfolioTracker
        tracker = PortfolioTracker()

        result = tracker.update_after_sell("999999", 10.0, 0.0)
        assert result is False
        mock_save.assert_not_called()
