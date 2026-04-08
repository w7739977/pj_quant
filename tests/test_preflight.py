"""
测试 scripts/preflight.py 各检查项（mock 外部依赖）
"""

import sys
import os
import json
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _make_stock_df(days=100, last_date=None):
    """生成模拟股票日线数据"""
    if last_date is None:
        last_date = datetime.now()
    dates = pd.date_range(end=last_date, periods=days, freq="D")
    return pd.DataFrame({
        "date": dates,
        "open": np.random.uniform(5, 50, days),
        "close": np.random.uniform(5, 50, days),
        "high": np.random.uniform(5, 50, days),
        "low": np.random.uniform(5, 50, days),
        "volume": np.random.uniform(1e6, 1e8, days),
        "pe_ttm": np.random.uniform(5, 50, days),
        "pb": np.random.uniform(0.5, 5, days),
        "turnover_rate": np.random.uniform(0.5, 10, days),
    })


class TestDataFreshness:
    """数据新鲜度检查"""

    @patch("data.storage.load_stock_daily")
    @patch("data.storage.list_cached_stocks")
    def test_pass(self, mock_list, mock_load):
        mock_list.return_value = [f"00000{i}" for i in range(10)]
        # 最新日期是昨天
        mock_load.return_value = _make_stock_df(last_date=datetime.now() - timedelta(days=0))

        from scripts.preflight import check_data_freshness
        result = check_data_freshness()
        assert result["pass"] is True

    @patch("data.storage.load_stock_daily")
    @patch("data.storage.list_cached_stocks")
    def test_fail(self, mock_list, mock_load):
        mock_list.return_value = [f"00000{i}" for i in range(10)]
        # 最新日期是30天前
        mock_load.return_value = _make_stock_df(last_date=datetime.now() - timedelta(days=30))

        from scripts.preflight import check_data_freshness
        result = check_data_freshness()
        assert result["pass"] is False


class TestDataAccuracy:
    """数据准确性检查"""

    @patch("data.storage.load_stock_daily")
    @patch("data.fetcher.fetch_realtime_tencent")
    def test_skip_on_network_error(self, mock_fetch, mock_load):
        mock_fetch.side_effect = Exception("Network error")

        from scripts.preflight import check_data_accuracy
        result = check_data_accuracy()
        assert result["pass"] is True
        assert result.get("skip") is True

    @patch("data.storage.load_stock_daily")
    @patch("data.fetcher.fetch_realtime_tencent")
    def test_pass_with_close_prices(self, mock_fetch, mock_load):
        # 在线价格和本地收盘价接近
        mock_fetch.return_value = {"price": 10.5}
        mock_load.return_value = pd.DataFrame({
            "date": [datetime.now()],
            "close": [10.49],
        })

        from scripts.preflight import check_data_accuracy
        result = check_data_accuracy()
        assert result["pass"] is True


class TestModelCheck:
    """模型状态检查"""

    @patch("scripts.preflight.os.path.exists")
    @patch("scripts.preflight.os.path.getmtime")
    def test_pass(self, mock_mtime, mock_exists):
        mock_exists.return_value = True
        mock_mtime.return_value = datetime.now().timestamp()  # 刚创建

        model_history = {"current": {"cv_r2_mean": 0.09, "date": "2026-04-08"}}
        with patch("builtins.open", MagicMock()):
            with patch("json.load", return_value=model_history):
                from scripts.preflight import check_model
                result = check_model()
                assert result["pass"] is True

    @patch("scripts.preflight.os.path.exists")
    def test_fail_missing(self, mock_exists):
        mock_exists.return_value = False

        from scripts.preflight import check_model
        result = check_model()
        assert result["pass"] is False
        assert "不存在" in result["msg"]

    @patch("scripts.preflight.os.path.exists")
    @patch("scripts.preflight.os.path.getmtime")
    def test_fail_low_r2(self, mock_mtime, mock_exists):
        mock_exists.return_value = True
        mock_mtime.return_value = datetime.now().timestamp()

        model_history = {"current": {"cv_r2_mean": 0.001, "date": "2026-04-01"}}
        with patch("builtins.open", MagicMock()):
            with patch("json.load", return_value=model_history):
                from scripts.preflight import check_model
                result = check_model()
                assert result["pass"] is False
                assert "0.02" in result["msg"]
