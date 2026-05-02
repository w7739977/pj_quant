"""
历史情绪批量回填 — Parquet 新闻 → FinBERT → SQLite sentiment_history

执行: python3 -c "from data.sentiment_backfill import run_backfill; run_backfill()"
预估: 1500 日 × 100 条/日 = 15 万条 × FinBERT batch ~0.1s/32 = ~10 分钟（CPU）
"""
import os
import logging
import pandas as pd
import re
from collections import defaultdict
from data.sentiment_history import save_sentiment_batch
from sentiment.finbert_local import score_texts

logger = logging.getLogger(__name__)
NEWS_PARQUET_DIR = "data/historical_news_parquet"


def _extract_stock_codes(text: str, valid_codes: set) -> list:
    """从新闻文本中提取股票代码（6 位数字）"""
    if not text:
        return []
    # 简化版：找所有 6 位数字
    candidates = re.findall(r"\b\d{6}\b", text)
    return [c for c in candidates if c in valid_codes]


def process_one_date(parquet_path: str, valid_codes: set) -> int:
    """处理单日 parquet → sentiment_history"""
    df = pd.read_parquet(parquet_path)
    if df.empty:
        return 0

    # 1. 给每条新闻打分（一次性 batch）
    texts = df["text"].fillna("").tolist()
    scores = score_texts(texts, batch_size=32)

    # 2. 给每条新闻匹配股票代码
    code_aggregates = defaultdict(list)  # code → [(score, source), ...]
    for i, row in df.iterrows():
        text = row["text"] if pd.notna(row["text"]) else ""
        score = scores[i]
        # 公告有 code，新闻需要从文本中匹配
        if row["code"]:
            matched_codes = [str(row["code"]).split(".")[0]]  # 000001.SZ → 000001
        else:
            matched_codes = _extract_stock_codes(text, valid_codes)
        for code in matched_codes:
            code_aggregates[code].append((score, row["source"]))

    # 3. 按股票聚合（同一天同一股票的多条新闻取均值）
    date = os.path.basename(parquet_path).replace(".parquet", "")
    date_str = f"{date[:4]}-{date[4:6]}-{date[6:8]}"

    rows = []
    for code, items in code_aggregates.items():
        scores_only = [s for s, _ in items]
        sources = list(set(src for _, src in items))
        rows.append({
            "date": date_str,
            "code": code,
            "score": round(sum(scores_only) / len(scores_only), 4),
            "news_count": len(items),
            "sources": sources,
        })

    return save_sentiment_batch(rows)


def run_backfill(limit: int = 0):
    """全量回填历史情绪"""
    from data.storage import list_cached_stocks
    valid_codes = set(list_cached_stocks())

    if not os.path.exists(NEWS_PARQUET_DIR):
        logger.warning(f"{NEWS_PARQUET_DIR} 不存在，请先运行 historical_news.batch_fetch_to_parquet")
        return

    files = sorted(f for f in os.listdir(NEWS_PARQUET_DIR) if f.endswith(".parquet"))
    if limit > 0:
        files = files[:limit]

    total_rows = 0
    for i, fname in enumerate(files, 1):
        path = os.path.join(NEWS_PARQUET_DIR, fname)
        try:
            n = process_one_date(path, valid_codes)
            total_rows += n
            if i % 50 == 0:
                logger.info(f"  [{i}/{len(files)}] 已写入 {total_rows} 行")
        except Exception as e:
            logger.warning(f"  {fname}: {e}")

    logger.info(f"回填完成: {total_rows} 条 sentiment_history 记录")
