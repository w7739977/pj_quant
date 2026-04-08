"""
测试 portfolio/allocator.py 中的 check_holdings()
"""

import sys
import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _mock_tracker(holdings: dict):
    """创建 mock tracker"""
    tracker = MagicMock()
    tracker.holdings = holdings
    return tracker


def _mock_realtime_df(prices: dict, names: dict = None):
    """创建 mock 实时行情 DataFrame"""
    names = names or {}
    rows = []
    for code, price in prices.items():
        rows.append({"code": code, "price": price, "name": names.get(code, code), "volume": 10000})
    return pd.DataFrame(rows)


class TestStopLossTrigger:
    """止损 -8% 触发"""

    @patch("data.fetcher.fetch_realtime_tencent_batch")
    def test_stop_loss_triggered(self, mock_fetch):
        from portfolio.allocator import check_holdings

        # 成本10元，当前9.1元 = -9% → 触发止损
        tracker = _mock_tracker({
            "000001": {"shares": 100, "avg_cost": 10.0, "buy_date": "2026-03-20"}
        })
        mock_fetch.return_value = _mock_realtime_df({"000001": 9.1}, {"000001": "平安银行"})

        actions = check_holdings(tracker, stop_loss_pct=-0.08)
        assert len(actions) == 1
        assert actions[0]["reason"] == "止损"
        assert actions[0]["pnl_pct"] < -8.0

    @patch("data.fetcher.fetch_realtime_tencent_batch")
    def test_stop_loss_boundary_not_triggered(self, mock_fetch):
        from portfolio.allocator import check_holdings

        # 成本10元，当前9.3元 = -7% → 未触及-8%线，不触发
        tracker = _mock_tracker({
            "000001": {"shares": 100, "avg_cost": 10.0, "buy_date": "2026-03-20"}
        })
        mock_fetch.return_value = _mock_realtime_df({"000001": 9.3})

        actions = check_holdings(tracker, stop_loss_pct=-0.08)
        assert len(actions) == 0


class TestTakeProfitTrigger:
    """止盈 +15% 触发"""

    @patch("data.fetcher.fetch_realtime_tencent_batch")
    def test_take_profit_triggered(self, mock_fetch):
        from portfolio.allocator import check_holdings

        # 成本10元，当前11.6元 = +16% → 触发止盈
        tracker = _mock_tracker({
            "000001": {"shares": 100, "avg_cost": 10.0, "buy_date": "2026-03-20"}
        })
        mock_fetch.return_value = _mock_realtime_df({"000001": 11.6}, {"000001": "平安银行"})

        actions = check_holdings(tracker, take_profit_pct=0.15)
        assert len(actions) == 1
        assert actions[0]["reason"] == "止盈"
        assert actions[0]["pnl_pct"] > 15.0


class TestTimeoutTrigger:
    """超时调仓（持有超20日且盈亏<3%）"""

    @patch("data.fetcher.fetch_realtime_tencent_batch")
    def test_timeout_triggered(self, mock_fetch):
        from portfolio.allocator import check_holdings

        # 25天前买入，成本10元，当前10.1元 = +1% → 超时调仓
        buy_date = (datetime.now() - timedelta(days=25)).strftime("%Y-%m-%d")
        tracker = _mock_tracker({
            "000001": {"shares": 100, "avg_cost": 10.0, "buy_date": buy_date}
        })
        mock_fetch.return_value = _mock_realtime_df({"000001": 10.1}, {"000001": "平安银行"})

        actions = check_holdings(tracker, max_holding_days=20)
        assert len(actions) == 1
        assert "超时调仓" in actions[0]["reason"]

    @patch("data.fetcher.fetch_realtime_tencent_batch")
    def test_timeout_not_triggered_profitable(self, mock_fetch):
        from portfolio.allocator import check_holdings

        # 25天前买入但涨了5%（>3%），不触发超时
        buy_date = (datetime.now() - timedelta(days=25)).strftime("%Y-%m-%d")
        tracker = _mock_tracker({
            "000001": {"shares": 100, "avg_cost": 10.0, "buy_date": buy_date}
        })
        mock_fetch.return_value = _mock_realtime_df({"000001": 10.5})

        actions = check_holdings(tracker, max_holding_days=20)
        assert len(actions) == 0


class TestNoTrigger:
    """正常持仓不触发"""

    @patch("data.fetcher.fetch_realtime_tencent_batch")
    def test_no_trigger_normal(self, mock_fetch):
        from portfolio.allocator import check_holdings

        # 持仓10天，-5%（未触止损-8%），不触发
        buy_date = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d")
        tracker = _mock_tracker({
            "000001": {"shares": 100, "avg_cost": 10.0, "buy_date": buy_date}
        })
        mock_fetch.return_value = _mock_realtime_df({"000001": 9.5})

        actions = check_holdings(
            tracker,
            stop_loss_pct=-0.08,
            take_profit_pct=0.15,
            max_holding_days=20,
        )
        assert len(actions) == 0
