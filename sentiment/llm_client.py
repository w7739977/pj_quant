"""统一 LLM 调用入口 — 主备 fallback

设计:
  - 所有 chat completion 调用走此 helper
  - 按 LLM_PROVIDERS 顺序尝试（DeepSeek 主，GLM 备）
  - 全部失败返回 None，调用方负责降级（跳过 AI 解读 / 用模板替代）

用法:
  from sentiment.llm_client import chat_completion
  reply = chat_completion(
      [{"role": "user", "content": "你好"}],
      max_tokens=100,
  )
  if reply is None:
      # LLM 全失败，降级处理
      ...

依赖:
  config.settings.LLM_PROVIDERS（主备列表，OpenAI 兼容 API）
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


def chat_completion(
    messages: list,
    temperature: float = 0.3,
    max_tokens: int = 200,
    timeout: int = 15,
    model: Optional[str] = None,
) -> Optional[str]:
    """OpenAI 兼容 chat/completions 调用，主备 fallback

    Parameters
    ----------
    messages : OpenAI messages 列表，[{"role": "user", "content": "..."}]
    temperature, max_tokens, timeout : 透传到 API
    model : 指定模型名（None=各 provider 默认）

    Returns
    -------
    str : LLM 回复内容（已 strip）
    None : 所有 provider 都失败（调用方需降级）
    """
    if not messages:
        return None
    try:
        import requests
    except ImportError:
        logger.error("requests 未安装")
        return None

    try:
        from config.settings import LLM_PROVIDERS
    except ImportError:
        logger.error("config.settings.LLM_PROVIDERS 未配置")
        return None

    if not LLM_PROVIDERS:
        return None

    last_error = None
    for provider in LLM_PROVIDERS:
        name = provider.get("name", "unknown")
        url = provider.get("url", "").rstrip("/")
        key = provider.get("key", "")
        prov_model = model or provider.get("model")
        if not url or not key or not prov_model:
            logger.debug(f"LLM provider [{name}] 配置不全，跳过")
            continue
        try:
            resp = requests.post(
                f"{url}/chat/completions",
                headers={"Authorization": f"Bearer {key}",
                         "Content-Type": "application/json"},
                json={
                    "model": prov_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"].get("content", "").strip()
            if content:
                if name != LLM_PROVIDERS[0].get("name"):
                    # 主源失败用了 fallback，记 warning 让运维知道
                    logger.warning(f"LLM 主源失败，已 fallback 到 [{name}]")
                return content
            logger.debug(f"LLM [{name}] 返回空内容，尝试下一个 provider")
        except Exception as e:
            last_error = e
            logger.warning(f"LLM [{name}] 调用失败: {e}")
            continue

    logger.error(f"所有 LLM provider 都失败，最后错误: {last_error}")
    return None


def chat_simple(prompt: str, **kwargs) -> Optional[str]:
    """单 prompt 简化版（自动包成 user message）"""
    return chat_completion([{"role": "user", "content": prompt}], **kwargs)
