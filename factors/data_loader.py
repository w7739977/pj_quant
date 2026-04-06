"""
因子数据获取模块

获取全 A 股的行情 + 基本面数据，用于因子计算。
数据源: AKShare + Baostock
"""

import pandas as pd
import numpy as np
import akshare as ak
import baostock as bs
import logging
from datetime import datetime, timedelta
from data.storage import save_daily_data, load_daily_data

logger = logging.getLogger(__name__)


# ============ 股票池筛选 ============

def get_small_cap_stocks(min_cap: float = 5e8, max_cap: float = 5e9) -> pd.DataFrame:
    """
    获取小市值股票池（AKShare 优先，Baostock 备份）

    Parameters
    ----------
    min_cap : float  最小市值（元），默认 5 亿
    max_cap : float  最大市值（元），默认 50 亿

    Returns
    -------
    DataFrame: columns [code, name, market_cap, industry]
    """
    # AKShare 优先
    try:
        df = ak.stock_zh_a_spot_em()
        df = df.rename(columns={
            "代码": "code", "名称": "name", "总市值": "market_cap",
            "流通市值": "float_cap", "市盈率-动态": "pe_ttm",
            "市净率": "pb", "涨跌幅": "change_pct",
            "成交量": "volume", "换手率": "turnover_rate",
        })
        # 过滤条件
        df = df[df["market_cap"].notna()]
        df["market_cap"] = pd.to_numeric(df["market_cap"], errors="coerce")
        df = df[(df["market_cap"] >= min_cap) & (df["market_cap"] <= max_cap)]

        # 排除 ST、*ST、退市股
        df = df[~df["name"].str.contains("ST|退|N", na=False)]

        # 排除北交所（8开头）和科创板（688开头）— 小资金开通门槛
        df = df[~df["code"].str.startswith(("8", "688"))]

        # 排除停牌股（成交量为0）
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
        df = df[df["volume"] > 0]

        df = df.sort_values("market_cap").reset_index(drop=True)
        logger.info(f"小市值股票池: {len(df)} 只 ({min_cap/1e8:.0f}~{max_cap/1e8:.0f}亿)")
        return df
    except Exception as e:
        logger.warning(f"AKShare 获取股票池失败: {e}")

    # Baostock 备份: 获取股票列表 + 基本面筛选
    try:
        df = _baostock_stock_pool(min_cap, max_cap)
        if not df.empty:
            return df
    except Exception as e:
        logger.warning(f"Baostock 获取股票池失败: {e}")

    return pd.DataFrame()


def _baostock_stock_pool(min_cap: float, max_cap: float) -> pd.DataFrame:
    """Baostock 备份: 获取小市值股票池"""
    lg = bs.login()
    if lg.error_code != "0":
        raise ConnectionError(f"Baostock login failed: {lg.error_msg}")

    today = datetime.now().strftime("%Y-%m-%d")

    # 获取最近交易日的基本面数据
    rs = bs.query_stock_basic()
    stock_rows = []
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        # 只取正常上市股票
        if row[5] == "1":  # type=1 表示上市
            stock_rows.append({"code": row[1].split(".")[-1], "name": row[2]})

    bs.logout()

    if not stock_rows:
        return pd.DataFrame()

    stock_df = pd.DataFrame(stock_rows)
    # 排除 ST、北交所、科创板
    stock_df = stock_df[~stock_df["name"].str.contains("ST|退|N", na=False)]
    stock_df = stock_df[~stock_df["code"].str.startswith(("8", "688"))]

    logger.info(f"Baostock 股票列表: {len(stock_df)} 只（无法获取市值，返回全部）")
    stock_df["market_cap"] = 0  # Baostock 无实时市值
    return stock_df.reset_index(drop=True)


def get_stock_daily(symbol: str, start_date: str, end_date: str = None) -> pd.DataFrame:
    """
    获取个股日线行情（前复权）

    Parameters
    ----------
    symbol : str  股票代码，如 "000001"
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # AKShare
    try:
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
            df = df.sort_values("date").reset_index(drop=True)
            return df
    except Exception:
        pass

    # Baostock 备份
    try:
        lg = bs.login()
        prefix = "sh" if symbol.startswith("6") else "sz"
        rs = bs.query_history_k_data_plus(
            f"{prefix}.{symbol}",
            "date,open,high,low,close,volume,turn,pctChg",
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="2",
        )
        rows = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        bs.logout()
        if rows:
            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "turnover", "pct_chg"])
            for c in ["open", "high", "low", "close", "volume", "turnover", "pct_chg"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])
            return df.sort_values("date").reset_index(drop=True)
    except Exception:
        pass

    return pd.DataFrame()


def get_stock_fundamentals(symbols: list) -> pd.DataFrame:
    """
    获取多只股票的基本面数据

    Returns
    -------
    DataFrame: [code, pe, pb, roe, revenue_growth, ...]
    """
    try:
        # 用实时行情接口获取 PE/PB
        df_all = ak.stock_zh_a_spot_em()
        df_all = df_all.rename(columns={
            "代码": "code", "市盈率-动态": "pe_ttm", "市净率": "pb",
            "总市值": "market_cap", "换手率": "turnover_rate",
            "量比": "volume_ratio", "市销率": "ps_ttm",
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
