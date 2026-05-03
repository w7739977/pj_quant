"""LLM client 主备 fallback 单测"""
import sys
import os
import pytest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sentiment.llm_client import chat_completion, chat_simple


def _mock_resp(content: str, status: int = 200):
    """构造 mock response"""
    resp = MagicMock()
    resp.status_code = status
    resp.raise_for_status = MagicMock(return_value=None) if status == 200 else \
        MagicMock(side_effect=Exception(f"HTTP {status}"))
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return resp


@pytest.fixture
def mock_providers(monkeypatch):
    """注入 mock provider 列表"""
    providers = [
        {"name": "primary", "url": "https://primary.test/v1",
         "key": "key1", "model": "model-1"},
        {"name": "fallback", "url": "https://fallback.test/v1",
         "key": "key2", "model": "model-2"},
    ]
    monkeypatch.setattr("config.settings.LLM_PROVIDERS", providers)
    yield providers


def test_primary_success(mock_providers):
    """主源成功直接返回"""
    with patch("requests.post") as mock_post:
        mock_post.return_value = _mock_resp("primary 回复")
        result = chat_simple("测试")
    assert result == "primary 回复"
    assert mock_post.call_count == 1
    # 确认调用的是 primary URL
    assert "primary.test" in mock_post.call_args[0][0]


def test_primary_fail_fallback_success(mock_providers):
    """主源失败，自动 fallback 到备源"""
    with patch("requests.post") as mock_post:
        mock_post.side_effect = [
            Exception("primary 网络超时"),
            _mock_resp("fallback 回复"),
        ]
        result = chat_simple("测试")
    assert result == "fallback 回复"
    assert mock_post.call_count == 2


def test_all_providers_fail_returns_none(mock_providers):
    """所有 provider 失败返回 None"""
    with patch("requests.post") as mock_post:
        mock_post.side_effect = Exception("全网络故障")
        result = chat_simple("测试")
    assert result is None
    assert mock_post.call_count == 2  # 主备都试过


def test_empty_messages_returns_none(mock_providers):
    """空 messages 直接返回 None，不发请求"""
    with patch("requests.post") as mock_post:
        result = chat_completion([])
    assert result is None
    assert mock_post.call_count == 0


def test_provider_missing_key_skipped(monkeypatch):
    """缺 key 的 provider 自动跳过"""
    providers = [
        {"name": "broken", "url": "https://x.test/v1", "key": "", "model": "m"},
        {"name": "good", "url": "https://y.test/v1", "key": "valid", "model": "m"},
    ]
    monkeypatch.setattr("config.settings.LLM_PROVIDERS", providers)
    with patch("requests.post") as mock_post:
        mock_post.return_value = _mock_resp("good 回复")
        result = chat_simple("测试")
    assert result == "good 回复"
    assert mock_post.call_count == 1
    assert "y.test" in mock_post.call_args[0][0]


def test_empty_content_falls_through(mock_providers):
    """主源返回空内容继续尝试 fallback"""
    with patch("requests.post") as mock_post:
        mock_post.side_effect = [
            _mock_resp(""),  # primary 返回空
            _mock_resp("fallback 内容"),
        ]
        result = chat_simple("测试")
    assert result == "fallback 内容"
    assert mock_post.call_count == 2


def test_chat_completion_with_messages_format(mock_providers):
    """messages 格式直接传入"""
    with patch("requests.post") as mock_post:
        mock_post.return_value = _mock_resp("ok")
        result = chat_completion(
            [{"role": "system", "content": "你是助手"},
             {"role": "user", "content": "你好"}],
        )
    assert result == "ok"
    body = mock_post.call_args.kwargs["json"]
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "system"
