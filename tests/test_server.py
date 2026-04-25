"""测试 server.py FastAPI 端点"""

import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 必须在 import server 之前设置强 token
os.environ["WEB_TOKEN"] = "test-token-only-for-ci"

from server import app
from fastapi.testclient import TestClient

client = TestClient(app)
TEST_TOKEN = "test-token-only-for-ci"


class TestAuth:
    """鉴权测试"""

    def test_status_unauthorized(self):
        resp = client.get("/api/status")
        assert resp.status_code == 401

    def test_status_authorized(self):
        resp = client.get(f"/api/status?token={TEST_TOKEN}")
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
        resp = client.get(f"/?token={TEST_TOKEN}")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "持仓" in resp.text

    def test_html_escaping(self):
        """XSS: reason 含 <script> 时应被转义"""
        import json
        # 写一个含 XSS 的 fake signal 文件
        today_str = __import__("datetime").datetime.now().strftime("%Y-%m-%d")
        signal_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "signals")
        os.makedirs(signal_dir, exist_ok=True)
        signal_path = os.path.join(signal_dir, f"{today_str}.json")
        fake_signal = {
            "buy_signals": [{"code": '000001"><script>alert(1)</script>',
                             "reason": '<script>alert("xss")</script>',
                             "shares": 100, "price": 10.0}],
            "sell_signals": [],
        }
        with open(signal_path, "w") as f:
            json.dump(fake_signal, f)

        resp = client.get(f"/?token={TEST_TOKEN}")
        assert "&lt;script&gt;" in resp.text
        assert '<script>alert' not in resp.text


class TestSync:
    """同步操作"""

    @patch("portfolio.trade_utils.estimate_sell_cost", return_value=5.0)
    @patch("portfolio.trade_utils.estimate_buy_cost", return_value=5.0)
    def test_sync_buy(self, mock_buy_cost, mock_sell_cost):
        resp = client.post(
            f"/api/sync?token={TEST_TOKEN}",
            json={
                "executed_buys": [{"code": "000001", "shares": 100, "price": 10.5}],
                "executed_sells": [],
                "client_request_id": "test-buy-001",
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
            f"/api/sync?token={TEST_TOKEN}",
            json={
                "executed_buys": [],
                "executed_sells": [{"code": "999999", "price": 10.0}],
                "client_request_id": "test-sell-nonexist-001",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["errors"]) > 0

    @patch("portfolio.trade_utils.estimate_sell_cost", return_value=5.0)
    @patch("portfolio.trade_utils.estimate_buy_cost", return_value=5.0)
    def test_sync_idempotent(self, mock_buy_cost, mock_sell_cost):
        """同一 client_request_id 第二次应返回 idempotent"""
        body = {
            "executed_buys": [{"code": "000002", "shares": 100, "price": 5.0}],
            "executed_sells": [],
            "client_request_id": "idempotent-test-unique-12345",
        }
        resp1 = client.post(f"/api/sync?token={TEST_TOKEN}", json=body)
        assert resp1.status_code == 200

        resp2 = client.post(f"/api/sync?token={TEST_TOKEN}", json=body)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert data2.get("idempotent") is True
