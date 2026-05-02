"""
每日情绪增量更新（cron 调用）

每天 16:00 收盘后跑（与 evolve 错开）:
  1. 拉今日新闻（Tushare 当日）
  2. FinBERT 打分
  3. 入库 sentiment_history

crontab:
  30 16 * * 1-5 cd /path/to/pj_quant && python3 scripts/sentiment_daily.py >> logs/sentiment.log 2>&1
"""
import os
import sys
import re
import logging
from datetime import datetime
from collections import defaultdict

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def run_daily():
    today_compact = datetime.now().strftime("%Y%m%d")
    today_dashed = datetime.now().strftime("%Y-%m-%d")

    # === 1. 拉今日新闻 ===
    from data.historical_news import (
        fetch_news_by_date, fetch_announcements_by_date, _init_tushare,
    )
    pro = _init_tushare()

    anns = fetch_announcements_by_date(pro, today_compact)
    news = fetch_news_by_date(pro, today_compact, src="sina")

    # 合并标准化
    combined = []
    if not anns.empty:
        anns_r = anns.rename(columns={"ann_date": "date", "ts_code": "code", "title": "text"})
        anns_r["source"] = "公告"
        combined.append(anns_r[["date", "code", "text", "source"]])
    if not news.empty:
        news_r = news.rename(columns={"datetime": "date", "title": "text"})
        news_r["code"] = ""
        news_r["source"] = "新闻"
        combined.append(news_r[["date", "code", "text", "source"]])

    if not combined:
        logger.info(f"{today_dashed} 无新闻数据")
        return

    import pandas as pd
    df = pd.concat(combined, ignore_index=True)
    logger.info(f"拉取 {today_dashed}: {len(df)} 条新闻/公告")

    # === 2. FinBERT 打分 ===
    from sentiment.finbert_local import score_texts

    texts = df["text"].fillna("").tolist()
    scores = score_texts(texts, batch_size=32)

    # === 3. 匹配股票代码 + 聚合 ===
    from data.storage import list_cached_stocks
    valid_codes = set(list_cached_stocks())

    code_aggregates = defaultdict(list)
    for i, row in df.iterrows():
        text = row["text"] if pd.notna(row["text"]) else ""
        score = scores[i]
        if row["code"]:
            matched_codes = [str(row["code"]).split(".")[0]]
        else:
            candidates = re.findall(r"\b\d{6}\b", text)
            matched_codes = [c for c in candidates if c in valid_codes]
        for code in matched_codes:
            code_aggregates[code].append((score, row["source"]))

    # === 4. 入库 ===
    from data.sentiment_history import save_sentiment_batch

    rows = []
    for code, items in code_aggregates.items():
        scores_only = [s for s, _ in items]
        sources = list(set(src for _, src in items))
        rows.append({
            "date": today_dashed,
            "code": code,
            "score": round(sum(scores_only) / len(scores_only), 4),
            "news_count": len(items),
            "sources": sources,
        })

    n = save_sentiment_batch(rows)
    logger.info(f"入库 {n} 条 sentiment_history 记录 ({today_dashed})")


if __name__ == "__main__":
    run_daily()
