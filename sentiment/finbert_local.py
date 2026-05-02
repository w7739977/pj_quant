"""
FinBERT-Chinese 本地推理 — 零限流、零成本、CPU 批处理 ~100ms / 32 条

模型: yiyanghkust/finbert-tone-chinese
首次运行会下载 ~400MB 到 ~/.cache/huggingface/

用法:
  from sentiment.finbert_local import score_texts, score_one
  scores = score_texts(["茅台业绩超预期", "暴雷股退市"])
  # 输出: [0.92, -0.85]
"""
import os
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# 全局单例（模型加载一次常驻）
_PIPELINE = None
_MODEL_NAME = os.getenv("FINBERT_MODEL", "yiyanghkust/finbert-tone-chinese")


def _get_pipeline():
    """懒加载 + 单例"""
    global _PIPELINE
    if _PIPELINE is None:
        try:
            from transformers import pipeline
            logger.info(f"加载 FinBERT 模型: {_MODEL_NAME}")
            _PIPELINE = pipeline(
                "sentiment-analysis",
                model=_MODEL_NAME,
                device=-1,  # CPU; 有 GPU 改为 0
                truncation=True,
                max_length=128,  # 标题足够，长内容截断
            )
            logger.info("FinBERT 加载完成")
        except Exception as e:
            logger.error(f"FinBERT 加载失败: {e}")
            raise
    return _PIPELINE


def score_texts(texts: List[str], batch_size: int = 32) -> List[float]:
    """
    批量打分文本 → 情绪分数 [-1, 1]

    Parameters
    ----------
    texts : 文本列表（标题或短句）
    batch_size : 批次大小（CPU 32, GPU 64-128）

    Returns
    -------
    list of float, 长度等于 texts。失败的位置返回 0.0。
    """
    if not texts:
        return []
    try:
        clf = _get_pipeline()
    except Exception:
        return [0.0] * len(texts)

    scores = []
    try:
        # transformers pipeline 自动 batch
        results = clf(texts, batch_size=batch_size)
        for r in results:
            label = r.get("label", "").lower()
            score = float(r.get("score", 0.5))
            # 标签映射 (finbert-tone-chinese: positive/negative/neutral)
            if "positive" in label or "pos" in label:
                scores.append(score)  # 0.5~1.0
            elif "negative" in label or "neg" in label:
                scores.append(-score)
            else:
                scores.append(0.0)  # neutral
    except Exception as e:
        logger.warning(f"FinBERT 推理失败: {e}")
        return [0.0] * len(texts)
    return scores


def score_one(text: str) -> float:
    """单文本打分快捷方法"""
    if not text:
        return 0.0
    return score_texts([text])[0]


def is_available() -> bool:
    """检查 FinBERT 是否可用（首次会下载，可能慢）"""
    try:
        _get_pipeline()
        return True
    except Exception:
        return False
