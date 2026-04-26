"""
因子数据获取模块

获取全 A 股的行情 + 基本面数据，用于因子计算。
数据源优先级: 本地 SQLite 缓存 → AKShare → Baostock
"""

import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta
from data.storage import load_stock_daily, list_cached_stocks

logger = logging.getLogger(__name__)


# ============ 股票池筛选 ============

def get_small_cap_stocks(min_cap: float = 5e8, max_cap: float = 5e9) -> pd.DataFrame:
    """
    获取小市值股票池（汇总表 → 腾讯 API → AKShare → BaoStock 多级降级）

    Parameters
    ----------
    min_cap : float  最小市值（元），默认 5 亿
    max_cap : float  最大市值（元），默认 50 亿

    Returns
    -------
    DataFrame: columns [code, name, market_cap, industry]
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
    if len(cached) > 100:
        # 本地有足够数据，直接用
        stock_df = pd.DataFrame({"code": cached})
        # 尝试用腾讯批量接口获取实时市值
        try:
            from data.fetcher import fetch_realtime_tencent_batch
            # 分批获取（腾讯每次最多约100只）
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
                stock_df = stock_df.merge(rt_df[["code", "market_cap"]], on="code", how="left")
                stock_df["market_cap"] = stock_df["market_cap"].fillna(0) * 1e8  # 亿→元

                # 按市值过滤
                stock_df = stock_df[
                    (stock_df["market_cap"] >= min_cap) & (stock_df["market_cap"] <= max_cap)
                ]
                stock_df = stock_df.sort_values("market_cap").reset_index(drop=True)
                logger.info(f"本地+腾讯: {len(stock_df)} 只 ({min_cap/1e8:.0f}~{max_cap/1e8:.0f}亿)")
                return stock_df
        except Exception as e:
            logger.warning(f"腾讯批量行情失败: {e}")

        # 无法获取市值，返回全部（无市值过滤）
        stock_df["market_cap"] = 0
        logger.info(f"本地缓存股票池: {len(stock_df)} 只（无市值过滤）")
        return stock_df

    # AKShare 备用
    try:
        import akshare as ak
        df = ak.stock_zh_a_spot_em()
        df = df.rename(columns={
            "代码": "code", "名称": "name", "总市值": "market_cap",
            "流通市值": "float_cap", "市盈率-动态": "pe_ttm",
            "市净率": "pb", "涨跌幅": "change_pct",
            "成交量": "volume", "换手率": "turnover_rate",
        })
        df = df[df["market_cap"].notna()]
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        df = df[(df["market_cap"] >= min_cap) & (df["market_cap"] <= max_cap)]
        df = df[~df["name"].str.contains("ST|退|N", na=False)]
        df = df[~df["code"].str.startswith(("8", "688"))]
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df = df[df["volume"] > 0]
        df = df.sort_values("market_cap").reset_index(drop=True)
        logger.info(f"AKShare股票池: {len(df)} 只 ({min_cap/1e8:.0f}~{max_cap/1e8:.0f}亿)")
        return df
    except Exception as e:
        logger.warning(f"AKShare 获取股票池失败: {e}")

    # BaoStock 最后备份
    try:
        df = _baostock_stock_pool()
        if not df.empty:
            return df
    except Exception as e:
        logger.warning(f"Baostock 获取股票池失败: {e}")

    return pd.DataFrame()


def _baostock_stock_pool() -> pd.DataFrame:
    """BaoStock 备份: 获取股票列表"""
    import baostock as bs
    lg = bs.login()
    if lg.error_code != "0":
        raise ConnectionError(f"Baostock login failed: {lg.error_msg}")

    rs = bs.query_stock_basic()
    stock_rows = []
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        # row: [code, name, ipoDate, outDate, type, tradeStatus]
        code = row[0] if len(row) > 0 else ""
        name = row[1] if len(row) > 1 else ""
        stype = row[4] if len(row) > 4 else ""
        trade_status = row[5] if len(row) > 5 else ""
        # type=1(股票) + tradeStatus=1(上市)
        if stype == "1" and trade_status == "1":
            pure_code = code.split(".")[-1] if "." in code else code
            stock_rows.append({"code": pure_code, "name": name})

    bs.logout()

    if not stock_rows:
        return pd.DataFrame()

    stock_df = pd.DataFrame(stock_rows)
    stock_df = stock_df[~stock_df["name"].str.contains("ST|退|N", na=False)]
    stock_df = stock_df[~stock_df["code"].str.startswith(("8", "688"))]

    logger.info(f"BaoStock 股票列表: {len(stock_df)} 只（无法获取市值，返回全部）")
    stock_df["market_cap"] = 0
    return stock_df.reset_index(drop=True)


def get_stock_daily(symbol: str, start_date: str, end_date: str = None) -> pd.DataFrame:
    """
    获取个股日线行情（优先本地缓存，再网络获取）

    Parameters
    ----------
    symbol : str  股票代码，如 "000001"
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # 优先读本地 SQLite 缓存
    df = load_stock_daily(symbol)
    if not df.empty:
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]
        if not df.empty:
            return df.reset_index(drop=True)

    # AKShare
    try:
        import akshare as ak
        df = ak.stock_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""),
            adjust="qfq"
        )
        if df is not None and len(df) > 0:
            df = df.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume",
                "换手率": "turnover", "涨跌幅": "pct_chg",
            })
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True)
    except Exception:
        pass

    # Baostock 备份
    try:
        import baostock as bs
        from data.bulk_fetcher import fetch_stock_daily as bs_fetch
        lg = bs.login()
        df = bs_fetch(symbol, start_date, end_date)
        bs.logout()
        if not df.empty:
            return df
    except Exception:
        pass

    return pd.DataFrame()


def get_stock_fundamentals(symbols: list) -> pd.DataFrame:
    """
    获取多只股票的基本面数据

    Returns
    -------
    DataFrame: [code, pe, pb, market_cap, turnover_rate, volume_ratio]
    """
    # 优先用腾讯批量接口
    try:
        from data.fetcher import fetch_realtime_tencent_batch
        all_rt = []
        for i in range(0, len(symbols), 100):
            batch = symbols[i:i+100]
            rt = fetch_realtime_tencent_batch(batch)
            if not rt.empty:
                all_rt.append(rt)
        if all_rt:
            result = pd.concat(all_rt, ignore_index=True)
            result = result.rename(columns={"total_market_cap": "market_cap"})
            for c in ["pe_ttm", "pb", "market_cap", "turnover_rate", "volume_ratio"]:
                if c in result.columns:
                    result[c] = pd.to_numeric(result[c], errors="coerce")
            avail_cols = [c for c in ["code", "pe_ttm", "pb", "market_cap", "turnover_rate", "volume_ratio"] if c in result.columns]
            return result[avail_cols].reset_index(drop=True)
    except Exception as e:
        logger.warning(f"腾讯基本面获取失败: {e}")

    # AKShare 备用
    try:
        import akshare as ak
        df_all = ak.stock_zh_a_spot_em()
        df_all = df_all.rename(columns={
            "代码": "code", "市盈率-动态": "pe_ttm", "市净率": "pb",
            "总市值": "market_cap", "换手率": "turnover_rate",
            "量比": "volume_ratio",
        })

        result = df_all[df_all["code"].isin(symbols)][
            ["code", "pe_ttm", "pb", "market_cap", "turnover_rate", "volume_ratio"]
        ].copy()

        for c in ["pe_ttm", "pb", "market_cap", "turnover_rate", "volume_ratio"]:
            result[c] = pd.to_numeric(result[c], errors="coerce")

        return result.reset_index(drop=True)
    except Exception as e:
        logger.error(f"获取基本面数据失败: {e}")
        return pd.DataFrame()
