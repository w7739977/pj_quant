"""
因子数据获取模块

获取全 A 股的行情 + 基本面数据，用于因子计算。
数据源优先级: 本地 SQLite 缓存 → AKShare → Baostock
"""

import sqlite3
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
from data.storage import load_stock_daily, list_cached_stocks

logger = logging.getLogger(__name__)


def _safe_table_name(code: str) -> str:
    """校验 stock code 格式，防止 SQL 注入"""
    if not code.isdigit() or len(code) != 6:
        raise ValueError(f"Invalid stock code: {code}")
    return f"stock_{code}"


# ============ 股票池筛选 ============

def get_small_cap_stocks(min_cap: float = 5e8, max_cap: float = 5e9) -> pd.DataFrame:
    """
    获取股票池（汇总表 → 本地SQLite → 腾讯API → AKShare 多级降级）

    Parameters
    ----------
    min_cap : float  最小市值（元），默认 5 亿（排除流动性枯竭的超小盘）
    max_cap : float  最大市值（元），默认 50 亿（聚焦小盘，业界 IC 最优区间）

    Returns
    -------
    DataFrame: columns [code, market_cap]
    """
    # M6: 优先用 latest_market_cap 汇总表（单条 SQL，毫秒级）
    try:
        from data.storage import query_market_cap_range, refresh_latest_market_cap
        results = query_market_cap_range(min_cap, max_cap)
        if not results:
            # 表不存在或为空 → 尝试 lazy refresh 一次（首次部署）
            n = refresh_latest_market_cap()
            if n > 0:
                results = query_market_cap_range(min_cap, max_cap)
                logger.info(f"汇总表 lazy refresh: {n} 只")

        if results:
            stock_df = pd.DataFrame(results).sort_values("market_cap").reset_index(drop=True)
            logger.info(f"汇总表 mv 筛选: {len(stock_df)} 只 ({min_cap/1e8:.0f}~{max_cap/1e8:.0f}亿)")
            return stock_df
    except Exception as e:
        logger.warning(f"汇总表查询失败: {e}")

    # 优先从本地缓存获取股票列表
    cached = list_cached_stocks()
    if not cached:
        logger.warning("本地无缓存股票，尝试 AKShare")
        return _fallback_akshare(min_cap, max_cap)

    # 优先从本地 SQLite 读 total_mv
    try:
        from config.settings import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        try:
            results = []
            for code in cached:
                table = _safe_table_name(code)
                try:
                    row = conn.execute(
                        f"SELECT total_mv FROM {table} "
                        f"WHERE total_mv IS NOT NULL ORDER BY date DESC LIMIT 1"
                    ).fetchone()
                    if row and row[0]:
                        mv = row[0] * 1e4  # 万元 → 元
                        if min_cap <= mv <= max_cap:
                            results.append({"code": code, "market_cap": mv})
                except Exception:
                    continue
        finally:
            conn.close()

        if results:
            stock_df = pd.DataFrame(results).sort_values("market_cap").reset_index(drop=True)
            logger.info(f"本地 total_mv 筛选: {len(stock_df)} 只 ({min_cap/1e8:.0f}~{max_cap/1e8:.0f}亿)")
            return stock_df
        else:
            logger.warning("本地 total_mv 全为空，fallback 到腾讯 API")
    except Exception as e:
        logger.warning(f"本地市值筛选失败: {e}")

    # 兜底: 腾讯批量 API
    return _fallback_tencent(cached, min_cap, max_cap)


def _fallback_tencent(cached: list, min_cap: float, max_cap: float) -> pd.DataFrame:
    """兜底: 腾讯批量实时 API 获取市值"""
    try:
        from data.fetcher import fetch_realtime_tencent_batch
        all_realtime = []
        for i in range(0, len(cached), 100):
            batch = cached[i:i+100]
            rt = fetch_realtime_tencent_batch(batch)
            if not rt.empty:
                all_realtime.append(rt)
        if all_realtime:
            rt_df = pd.concat(all_realtime, ignore_index=True)
            rt_df["total_market_cap"] = pd.to_numeric(rt_df.get("total_market_cap", 0), errors="coerce")
            rt_df = rt_df.rename(columns={"total_market_cap": "market_cap"})
            stock_df = pd.DataFrame({"code": cached})
            stock_df = stock_df.merge(rt_df[["code", "market_cap"]], on="code", how="left")
            stock_df["market_cap"] = stock_df["market_cap"].fillna(0) * 1e8  # 亿→元
            stock_df = stock_df[
                (stock_df["market_cap"] >= min_cap) & (stock_df["market_cap"] <= max_cap)
            ]
            stock_df = stock_df.sort_values("market_cap").reset_index(drop=True)
            logger.info(f"腾讯 API 兜底: {len(stock_df)} 只")
            return stock_df
    except Exception as e:
        logger.warning(f"腾讯批量行情失败: {e}")

    # AKShare 最后兜底
    return _fallback_akshare(min_cap, max_cap)


def _fallback_akshare(min_cap: float, max_cap: float) -> pd.DataFrame:
    """兜底: AKShare 获取股票池"""
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        df = df.rename(columns={
            "代码": "code", "名称": "name", "总市值": "market_cap",
        })
        df = df[df["market_cap"].notna()]
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        df = df[(df["market_cap"] >= min_cap) & (df["market_cap"] <= max_cap)]
        df = df[~df["name"].str.contains("ST|退|N", na=False)]
        df = df[~df["code"].str.startswith(("8", "688"))]
        df = df.sort_values("market_cap").reset_index(drop=True)
        logger.info(f"AKShare 股票池: {len(df)} 只 ({min_cap/1e8:.0f}~{max_cap/1e8:.0f}亿)")
        return df
    except Exception as e:
        logger.warning(f"AKShare 获取股票池失败: {e}")
        return pd.DataFrame()
