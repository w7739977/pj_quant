"""
LLM 情绪分析模块 - 双模型协作

流程:
1. glm-4-flash: 快速批量打情绪标签（高效、低成本）
2. glm-5: 深度推理关键新闻，提取投资逻辑，修正情绪分数
3. 规则模式: API 不可用时降级（关键词匹配）
"""

import re
import json
import requests
import logging
import numpy as np
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


# ============ 情绪关键词词库（规则模式降级用） ============

POSITIVE_WORDS = [
    "利好", "上涨", "涨停", "暴涨", "大涨", "突破", "新高", "业绩大增",
    "超预期", "增持", "回购", "分红", "牛市", "强势", "放量上涨", "资金流入",
    "订单", "中标", "获批", "签约", "合作", "创新高", "盈利", "增长",
    "复苏", "回暖", "政策支持", "刺激", "利好消息",
]

NEGATIVE_WORDS = [
    "利空", "下跌", "跌停", "暴跌", "大跌", "破位", "新低", "业绩下滑",
    "亏损", "减持", "退市", "违规", "处罚", "诉讼", "爆雷", "违约",
    "风险", "熊市", "弱势", "放量下跌", "资金流出", "负增长", "裁员",
    "破产", "质押", "冻结", "调查", "警告", "下调", "看空",
]


# ============ 新闻抓取 ============

def fetch_eastmoney_news() -> list:
    """抓取东方财富要闻"""
    try:
        url = "https://finance.eastmoney.com/a/czqyw.html"
        headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.encoding = "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")

        news = []
        for item in soup.select(".title a"):
            title = item.get_text(strip=True)
            if title:
                news.append({"title": title, "content": "", "source": "eastmoney"})
        return news[:30]
    except Exception as e:
        logger.warning(f"东方财富新闻抓取失败: {e}")
        return []


def fetch_all_news() -> list:
    """
    东方财富新闻聚合

    去重（标题相似度），优先保留有内容的条目
    """
    all_news = fetch_eastmoney_news()

    # 去重: 标题前10字符相同的只保留一条
    seen = set()
    unique = []
    for n in all_news:
        key = n["title"][:10]
        if key not in seen:
            seen.add(key)
            unique.append(n)

    return unique


def fetch_stock_news(symbol: str) -> list:
    """抓取个股相关新闻"""
    try:
        url = "https://search-api-web.eastmoney.com/search/jsonp"
        params = {
            "cb": "",
            "param": json.dumps({
                "uid": "", "keyword": symbol,
                "type": ["cmsArticleWebOld"],
                "client": "web", "clientType": "web", "clientVersion": "curr",
                "param": {"cmsArticleWebOld": {
                    "searchScope": "default", "sort": "default",
                    "pageIndex": 1, "pageSize": 10, "preTag": "", "postTag": "",
                }},
            }),
        }
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        text = re.sub(r'^.*?\(', '', resp.text).rstrip(')')
        data = json.loads(text)
        cms = data.get("result", {}).get("cmsArticleWebOld", [])
        # API 格式可能是 list 或 dict with "list" key
        if isinstance(cms, dict):
            articles = cms.get("list", [])
        else:
            articles = cms

        news = []
        for a in articles:
            title = a.get("title", "").replace("<em>", "").replace("</em>", "")
            if title:
                news.append({"title": title, "content": a.get("content", ""), "source": "eastmoney_stock"})
        return news
    except Exception as e:
        logger.warning(f"个股新闻抓取失败 {symbol}: {e}")
        return []


# ============ 底层 API 调用 ============

def _call_llm(model: str, prompt: str, max_tokens: int = 300, temperature: float = 0.1,
              timeout: int = 30) -> str:
    """调用智谱 GLM API，返回 content 文本，失败返回空字符串"""
    try:
        from config.settings import LLM_API_KEY, LLM_BASE_URL
    except ImportError:
        return ""

    if not LLM_API_KEY:
        return ""

    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=timeout,
        )
        result = resp.json()

        if "error" in result:
            logger.warning(f"GLM API 错误 ({model}): {result['error']}")
            return ""

        content = result["choices"][0]["message"].get("content", "").strip()
        return content
    except Exception as e:
        logger.warning(f"GLM API 调用失败 ({model}): {e}")
        return ""


def _parse_scores(content: str, expected_count: int):
    """从 LLM 回复中解析分数数组"""
    if not content:
        return None

    # 尝试提取 JSON 数组
    match = re.search(r'\[([-\d.,\s]+)\]', content)
    if match:
        try:
            scores = [float(x.strip()) for x in match.group(1).split(",")]
            while len(scores) < expected_count:
                scores.append(0.0)
            return [max(-1, min(1, s)) for s in scores[:expected_count]]
        except (ValueError, IndexError):
            pass

    # 降级: 逐个解析数字
    numbers = re.findall(r"[-+]?\d*\.?\d+", content)
    if numbers:
        scores = [float(n) for n in numbers]
        while len(scores) < expected_count:
            scores.append(0.0)
        return [max(-1, min(1, s)) for s in scores[:expected_count]]

    return None


# ============ 情绪分析 ============

def rule_based_sentiment(text: str) -> float:
    """规则情绪分析: 关键词匹配，返回 [-1, 1]"""
    if not text:
        return 0.0
    pos = sum(1 for w in POSITIVE_WORDS if w in text)
    neg = sum(1 for w in NEGATIVE_WORDS if w in text)
    total = pos + neg
    return (pos - neg) / total if total > 0 else 0.0


def flash_tag_sentiment(texts: list[str]) -> list[float]:
    """
    批量给文本打情绪分

    优先级: FinBERT 本地（零限流）→ GLM-4-flash → 关键词规则
    """
    if not texts:
        return []

    # 优先 FinBERT 本地推理
    try:
        from sentiment.finbert_local import score_texts, is_available
        if is_available():
            scores = score_texts(texts)
            if scores and any(s != 0 for s in scores):
                logger.debug(f"FinBERT 打分 {len(texts)} 条")
                return scores
    except Exception as e:
        logger.warning(f"FinBERT 调用失败，降级 GLM: {e}")

    # 降级 1: GLM-4-flash
    news_block = "\n".join(f"{i+1}. {t[:100]}" for i, t in enumerate(texts))
    prompt = f"""给以下{len(texts)}条A股相关新闻打情绪分，范围[-1,1]，-1最利空，1最利好。考虑对A股市场的实际影响程度。

新闻列表:
{news_block}

只回复一个JSON数组，如 [0.5, -0.3, ...]，不要其他内容。"""
    content = _call_llm("glm-4-flash", prompt, max_tokens=300, temperature=0.3)
    scores = _parse_scores(content, len(texts))

    if scores is not None:
        return scores

    # 降级 2: 关键词规则
    logger.info("LLM 全部失败，降级到规则模式")
    return [rule_based_sentiment(t) for t in texts]


def glm5_deep_analysis(news_with_scores: list):
    """
    第二阶段: glm-5 深度推理分析

    输入 flash 打过标签的新闻，让 glm-5 推理:
    1. 识别关键主题和风险点
    2. 验证/修正极端情绪分数
    3. 生成综合研判结论

    Returns
    -------
    dict: {analysis: str, adjusted_score: float, key_risks: list, highlights: list}
    """
    if not news_with_scores:
        return None

    # 只选情绪最极端的 5 条新闻给 glm-5 分析（节省 token）
    sorted_news = sorted(news_with_scores, key=lambda x: -abs(x["sentiment"]))[:5]
    news_block = "\n".join(
        f'{i+1}. [{n["sentiment"]:+.1f}] {n["title"]}'
        for i, n in enumerate(sorted_news)
    )

    prompt = f"""你是一位资深A股策略分析师。以下是今日最关键的财经新闻及初步情绪评分（由AI给出）。

请深度分析:
1. 这些新闻背后的主线逻辑是什么
2. 初步评分是否合理，如有偏差请给出修正建议
3. 对明日A股的操作建议

新闻:
{news_block}

请严格按以下JSON格式回复:
{{"theme": "今日市场主线（一句话）", "adjusted_score": 0.0, "analysis": "100字以内深度分析", "action": "加仓/减仓/持有/观望", "risks": ["风险1", "风险2"]}}"""

    content = _call_llm("glm-5", prompt, max_tokens=8000, temperature=0.3, timeout=180)

    if not content:
        return None

    # 尝试解析 JSON
    try:
        # 去掉可能的 markdown 代码块标记
        clean = re.sub(r'```json\s*', '', content)
        clean = re.sub(r'```\s*', '', clean)
        result = json.loads(clean)
        return result
    except json.JSONDecodeError:
        # json 解析失败，提取关键信息
        return {"analysis": content[:300], "adjusted_score": None}


# ============ 高层接口 ============

def analyze_market_sentiment() -> dict:
    """
    分析当日市场整体情绪（双模型协作）

    流程: flash 快速打标 → glm-5 深度推理 → 综合输出

    Returns
    -------
    dict: {score, positive_ratio, news_count, top_news, mode, deep_analysis}
    """
    news = fetch_all_news()
    if not news:
        return {"score": 0, "news_count": 0, "positive_ratio": 0, "top_news": [], "mode": "no_data"}

    texts = [n["title"] + " " + n.get("content", "") for n in news]

    # === 第一阶段: flash 快速打标 ===
    scores = flash_tag_sentiment(texts)
    mode = "flash"

    # === 第二阶段: glm-5 深度推理 ===
    news_with_scores = [
        {"title": n["title"], "sentiment": s}
        for n, s in zip(news, scores)
    ]

    deep = glm5_deep_analysis(news_with_scores)
    deep_analysis = None

    if deep:
        mode = "flash+glm5"
        deep_analysis = deep

        # 用 glm-5 的修正分数微调最终得分
        adjusted = deep.get("adjusted_score")
        if adjusted is not None and -1 <= adjusted <= 1:
            # 70% flash 均值 + 30% glm-5 修正（避免完全依赖单次推理）
            avg_flash = float(np.mean(scores))
            final_score = avg_flash * 0.7 + adjusted * 0.3
        else:
            final_score = float(np.mean(scores))
    else:
        final_score = float(np.mean(scores))

    pos_ratio = sum(1 for s in scores if s > 0) / len(scores)

    return {
        "score": round(final_score, 3),
        "news_count": len(news),
        "positive_ratio": round(pos_ratio, 3),
        "mode": mode,
        "top_news": [
            {"title": n["title"], "sentiment": round(s, 2)}
            for n, s in sorted(zip(news, scores), key=lambda x: -abs(x[1]))[:5]
        ],
        "deep_analysis": deep_analysis,
    }


def analyze_stock_sentiment(symbol: str, name: str = "") -> dict:
    """分析个股情绪（东方财富新闻源）"""
    news = fetch_stock_news(symbol)

    if not news:
        return {"code": symbol, "score": 0, "news_count": 0, "sources": "none"}

    titles = [n["title"] for n in news]
    scores = flash_tag_sentiment(titles)

    return {
        "code": symbol,
        "score": round(float(np.mean(scores)), 3),
        "news_count": len(news),
        "top_news": [
            {"title": n["title"], "sentiment": round(s, 2)}
            for n, s in sorted(zip(news, scores), key=lambda x: -abs(x[1]))[:3]
        ],
    }
