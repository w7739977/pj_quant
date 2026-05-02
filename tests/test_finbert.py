"""测试 FinBERT 本地推理"""
import pytest
from sentiment.finbert_local import score_texts, score_one


@pytest.fixture(scope="module")
def model_loaded():
    """首次加载模型（pytest 复用）"""
    from sentiment.finbert_local import is_available
    if not is_available():
        pytest.skip("FinBERT 不可用（首次运行需下载 ~400MB）")


def test_positive_news(model_loaded):
    """利好新闻应得正分"""
    score = score_one("贵州茅台业绩超预期，营收增长 30%")
    assert score > 0.3, f"利好应正分，实际 {score}"


@pytest.mark.xfail(
    reason="yiyanghkust/finbert-tone-chinese 训练语料偏研报（多正面/中性），"
           "对负面新闻识别能力差。详见 docs/optimization_backlog.md P1.6（替换模型）",
    strict=False,
)
def test_negative_news(model_loaded):
    """利空新闻应得负分（已知模型问题，标记 xfail）"""
    score = score_one("某股暴雷退市，投资者血本无归")
    assert score < -0.3, f"利空应负分，实际 {score}"


def test_batch_consistency(model_loaded):
    """批量与单条结果应一致"""
    texts = ["业绩大涨创新高", "亏损扩大跌停"]
    batch_scores = score_texts(texts)
    single_scores = [score_one(t) for t in texts]
    for b, s in zip(batch_scores, single_scores):
        assert abs(b - s) < 0.01


def test_empty_input():
    assert score_texts([]) == []
    assert score_one("") == 0.0
