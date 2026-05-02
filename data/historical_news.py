"""
历史新闻批量拉取 — Tushare 新闻流 + 公司公告

策略:
  1. 先拉财经新闻（覆盖广，但需匹配股票代码）
  2. 公司公告（精确到股票，质量最高）
  3. 券商研报（专业，时效低）
"""
import os
import time
import logging
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)
NEWS_PARQUET_DIR = "data/historical_news_parquet"


def _init_tushare():
    import tushare as ts
    from data.tushare_fundamentals import TUSHARE_TOKEN
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def fetch_news_by_date(pro, trade_date: str, src: str = "sina") -> pd.DataFrame:
    """
    按日期拉取财经新闻流
    src: sina/wallstreetcn/10jqka/eastmoney/yuncaijing
    """
    try:
        df = pro.news(start_date=f"{trade_date} 00:00:00",
                      end_date=f"{trade_date} 23:59:59",
                      src=src)
        return df
    except Exception as e:
        logger.warning(f"news {trade_date} {src}: {e}")
        return pd.DataFrame()


def fetch_announcements_by_date(pro, trade_date: str) -> pd.DataFrame:
    """按日期拉公告（覆盖全市场）"""
    try:
        df = pro.anns_d(start_date=trade_date, end_date=trade_date)
        return df
    except Exception:
        return pd.DataFrame()


def batch_fetch_to_parquet(start: str, end: str):
    """
    历史新闻批量拉取到 Parquet（避免重复抓取）

    用法: batch_fetch_to_parquet("20200101", "20260430")
    时间预估: ~1500 个交易日 × 2-3 秒/天 = 1.5 小时
    """
    os.makedirs(NEWS_PARQUET_DIR, exist_ok=True)
    pro = _init_tushare()

    dates = pro.trade_cal(exchange="SSE", is_open="1",
                          start_date=start, end_date=end)["cal_date"].tolist()

    for date in dates:
        out_path = os.path.join(NEWS_PARQUET_DIR, f"{date}.parquet")
        if os.path.exists(out_path):
            continue

        # 公告 + 新浪新闻聚合
        anns = fetch_announcements_by_date(pro, date)
        news = fetch_news_by_date(pro, date, src="sina")

        # 合并标准化
        combined = []
        if not anns.empty:
            anns_renamed = anns.rename(columns={"ann_date": "date", "ts_code": "code", "title": "text"})
            anns_renamed["source"] = "公告"
            combined.append(anns_renamed[["date", "code", "text", "source"]])
        if not news.empty:
            # news 没有 code 列，需要后续从文本中匹配股票
            news_renamed = news.rename(columns={"datetime": "date", "title": "text"})
            news_renamed["code"] = ""
            news_renamed["source"] = "新闻"
            combined.append(news_renamed[["date", "code", "text", "source"]])

        if combined:
            df_all = pd.concat(combined, ignore_index=True)
            df_all.to_parquet(out_path, index=False)
            ann_count = len(anns) if not anns.empty else 0
            news_count = len(news) if not news.empty else 0
            logger.info(f"  {date}: 公告{ann_count} + 新闻{news_count} → parquet")

        time.sleep(0.35)  # 避免限流
