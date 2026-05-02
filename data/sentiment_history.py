"""
情绪历史数据库 — 让 ML 训练能 JOIN 历史情绪

Schema:
  sentiment_history (date, code, score, news_count, sources, updated_at)
  PRIMARY KEY (date, code)

数据流:
  公司公告 (Tushare anns_d) ──┐
  券商研报 (Tushare report)   ├─→ FinBERT 批处理 → sentiment_history
  财经新闻 (东方财富)         ┘
"""

import os
import sqlite3
import json
import logging
import pandas as pd
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)
DB_PATH = "data/quant.db"


def _init_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sentiment_history (
            date TEXT,
            code TEXT,
            score REAL,
            news_count INTEGER DEFAULT 0,
            sources TEXT DEFAULT '[]',
            updated_at TEXT,
            PRIMARY KEY (date, code)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sent_date ON sentiment_history(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sent_code ON sentiment_history(code)")
    conn.commit()


def save_sentiment(date: str, code: str, score: float,
                   news_count: int = 0, sources: list = None) -> None:
    """单条 UPSERT"""
    conn = sqlite3.connect(DB_PATH)
    try:
        _init_table(conn)
        sources_json = json.dumps(sources or [], ensure_ascii=False)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR REPLACE INTO sentiment_history "
            "(date, code, score, news_count, sources, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, code, score, news_count, sources_json, now),
        )
        conn.commit()
    finally:
        conn.close()


def save_sentiment_batch(rows: list) -> int:
    """
    批量 UPSERT
    rows: [{date, code, score, news_count, sources}, ...]
    Returns: 成功条数
    """
    if not rows:
        return 0
    conn = sqlite3.connect(DB_PATH)
    try:
        _init_table(conn)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = [
            (r["date"], r["code"], r.get("score", 0.0),
             r.get("news_count", 0),
             json.dumps(r.get("sources", []), ensure_ascii=False),
             now)
            for r in rows
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO sentiment_history "
            "(date, code, score, news_count, sources, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            data,
        )
        conn.commit()
        return len(data)
    finally:
        conn.close()


def load_sentiment(code: str, start: str = None, end: str = None) -> pd.DataFrame:
    """读取某只股票历史情绪"""
    conn = sqlite3.connect(DB_PATH)
    try:
        sql = "SELECT date, code, score, news_count FROM sentiment_history WHERE code = ?"
        params = [code]
        if start:
            sql += " AND date >= ?"
            params.append(start)
        if end:
            sql += " AND date <= ?"
            params.append(end)
        sql += " ORDER BY date"
        return pd.read_sql(sql, conn, params=params)
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def load_sentiment_for_date(date: str) -> dict:
    """读取某一天全市场情绪 → {code: score}"""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "SELECT code, score FROM sentiment_history WHERE date = ?",
            (date,),
        )
        return {code: score for code, score in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def load_all_to_dict() -> dict:
    """一次性加载全部历史情绪到内存 → {(date, code): score}"""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("SELECT date, code, score FROM sentiment_history")
        return {(d, c): s for d, c, s in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def get_coverage() -> dict:
    """统计覆盖情况"""
    conn = sqlite3.connect(DB_PATH)
    try:
        result = conn.execute("""
            SELECT
                COUNT(*) as total_rows,
                COUNT(DISTINCT code) as unique_codes,
                COUNT(DISTINCT date) as unique_dates,
                MIN(date) as min_date,
                MAX(date) as max_date
            FROM sentiment_history
        """).fetchone()
        return {
            "total_rows": result[0],
            "unique_codes": result[1],
            "unique_dates": result[2],
            "min_date": result[3],
            "max_date": result[4],
        }
    finally:
        conn.close()
