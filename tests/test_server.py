"""
测试 server.py FastAPI 端点
"""

import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 必须在 import server 之前设置（防止 settings.py 加载 .env 失败）
os.environ.setdefault("WEB_TOKEN", "pj_quant_2026")

from server import app
from fastapi.testclient import TestClient

client = TestClient(app)


class TestAuth:
    """鉴权测试"""

    def test_status_unauthorized(self):
        resp = client.get("/api/status")
        assert resp.status_code == 401

    def test_status_authorized(self):
        resp = client.get("/api/status?token=pj_quant_2026")
        assert resp.status_code == 200
        data = resp.json()
        assert "holdings" in data
        assert "cash" in data

    def test_homepage_unauthorized(self):
        resp = client.get("/")
        assert resp.status_code == 401


class TestHomepage:
    """主页"""

    def test_returns_html(self):
        resp = client.get("/?token=pj_quant_2026")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "持仓" in resp.text


class TestSync:
    """同步操作"""

    @patch("portfolio.trade_utils.estimate_sell_cost", return_value=5.0)
    @patch("portfolio.trade_utils.estimate_buy_cost", return_value=5.0)
    def test_sync_buy(self, mock_buy_cost, mock_sell_cost):
        resp = client.post(
            "/api/sync?token=pj_quant_2026",
            json={
                "executed_buys": [{"code": "000001", "shares": 100, "price": 10.5}],
                "executed_sells": [],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert len(data["results"]) == 1

    @patch("portfolio.trade_utils.estimate_sell_cost", return_value=5.0)
    @patch("portfolio.trade_utils.estimate_buy_cost", return_value=5.0)
    def test_sync_sell_nonexistent(self, mock_buy_cost, mock_sell_cost):
        resp = client.post(
            "/api/sync?token=pj_quant_2026",
            json={
                "executed_buys": [],
                "executed_sells": [{"code": "999999", "price": 10.0}],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["errors"]) > 0
