"""
测试 scripts/postflight.py
"""

import sys
import os
import json
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestArchiveCreatesFile:
    """归档生成文件"""

    @patch("portfolio.allocator.get_stock_picks_live", return_value=[])
    @patch("portfolio.allocator.check_holdings", return_value=[])
    @patch("portfolio.tracker.PortfolioTracker")
    @patch("scripts.postflight._is_weekend", return_value=False)
    def test_creates_json(self, mock_weekend, mock_tracker_cls, mock_check, mock_picks, tmp_path):
        # Mock tracker
        tracker = MagicMock()
        tracker.holdings = {}
        tracker.cash = 20000.0
        mock_tracker_cls.return_value = tracker

        # 重定向归档目录
        from scripts import postflight
        original_dir = postflight._archive_dir
        postflight._archive_dir = lambda: str(tmp_path)

        try:
            postflight.run()

            today = datetime.now().strftime("%Y-%m-%d")
            archive_path = tmp_path / f"{today}.json"
            assert archive_path.exists()

            with open(archive_path) as f:
                data = json.load(f)
            assert "date" in data
            assert "holdings" in data
            assert "sell_signals" in data
            assert "buy_signals" in data
            assert "portfolio_value" in data
            assert "cash" in data
        finally:
            postflight._archive_dir = original_dir


class TestArchiveIdempotent:
    """幂等性"""

    @patch("portfolio.allocator.get_stock_picks_live", return_value=[])
    @patch("portfolio.allocator.check_holdings", return_value=[])
    @patch("portfolio.tracker.PortfolioTracker")
    @patch("scripts.postflight._is_weekend", return_value=False)
    def test_overwrites(self, mock_weekend, mock_tracker_cls, mock_check, mock_picks, tmp_path):
        tracker = MagicMock()
        tracker.holdings = {}
        tracker.cash = 20000.0
        mock_tracker_cls.return_value = tracker

        from scripts import postflight
        original_dir = postflight._archive_dir
        postflight._archive_dir = lambda: str(tmp_path)

        try:
            postflight.run()
            postflight.run()  # 第二次不报错

            today = datetime.now().strftime("%Y-%m-%d")
            assert (tmp_path / f"{today}.json").exists()
        finally:
            postflight._archive_dir = original_dir


class TestWeekendSkip:
    """周末跳过"""

    @patch("scripts.postflight._is_weekend", return_value=True)
    def test_skips(self, mock_weekend):
        from scripts import postflight
        # 只要不报错就说明跳过了
        postflight.run()


class TestBuySignalNotOverlap:
    """合理性检查: 买入信号不与持仓重复"""

    def test_warning_on_overlap(self, capsys):
        from scripts.postflight import _sanity_check

        archive = {
            "holdings": {"000001": {"shares": 100}},
            "buy_signals": [{"code": "000001", "shares": 100, "price": 10.0}],
            "sell_signals": [],
            "cash": 10000,
        }
        tracker = MagicMock()
        _sanity_check(archive, tracker, 3)

        captured = capsys.readouterr()
        assert "重复" in captured.out

    def test_pass_no_overlap(self, capsys):
        from scripts.postflight import _sanity_check

        archive = {
            "holdings": {"000001": {"shares": 100}, "600519": {"shares": 10}, "300750": {"shares": 50}},
            "buy_signals": [{"code": "002230", "shares": 100, "price": 15.0}],
            "sell_signals": [{"code": "000001", "reason": "止盈"}],
            "cash": 20000,
        }
        tracker = MagicMock()
        _sanity_check(archive, tracker, 3)

        captured = capsys.readouterr()
        assert "通过" in captured.out
