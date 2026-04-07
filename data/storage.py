"""
数据存储模块 - SQLite 本地缓存
"""

import sqlite3
import pandas as pd
import os
import logging

from config.settings import DB_PATH

logger = logging.getLogger(__name__)


def get_connection() -> sqlite3.Connection:
    """获取数据库连接"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    return conn


def save_daily_data(df: pd.DataFrame, symbol: str):
    """将日线数据追加存入 SQLite（增量，去重）"""
    conn = get_connection()
    table_name = f"etf_{symbol}"

    existing = pd.DataFrame()
    try:
        existing = pd.read_sql(f"SELECT date FROM {table_name}", conn)
    except Exception:
        pass

    if not existing.empty:
        existing_dates = set(pd.to_datetime(existing["date"]).dt.strftime("%Y-%m-%d"))
        df = df.copy()
        df["date_str"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        new_rows = df[~df["date_str"].isin(existing_dates)]
        new_rows = new_rows.drop(columns=["date_str"])
        if not new_rows.empty:
            new_rows.to_sql(table_name, conn, if_exists="append", index=False)
            logger.info(f"增量追加 {symbol}: +{len(new_rows)} 条")
        else:
            logger.info(f"{symbol} 数据已是最新")
    else:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
        logger.info(f"已保存 {symbol} 日线数据: {len(df)} 条 -> {table_name}")

    conn.close()


def load_daily_data(symbol: str) -> pd.DataFrame:
    """从 SQLite 加载日线数据"""
    conn = get_connection()
    table_name = f"etf_{symbol}"
    try:
        df = pd.read_sql(f"SELECT * FROM {table_name}", conn)
        df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def get_cached_date_range(symbol: str) -> tuple:
    """
    获取本地缓存数据的日期范围

    Returns
    -------
    (min_date, max_date) 字符串，无数据返回 (None, None)
    """
    conn = get_connection()
    table_name = f"etf_{symbol}"
    try:
        df = pd.read_sql(f"SELECT MIN(date) as mn, MAX(date) as mx FROM {table_name}", conn)
        if len(df) > 0 and df.iloc[0]["mn"] is not None:
            return str(df.iloc[0]["mn"]), str(df.iloc[0]["mx"])
    except Exception:
        pass
    finally:
        conn.close()
    return None, None


def save_backtest_result(df: pd.DataFrame, strategy_name: str):
    """保存回测结果"""
    conn = get_connection()
    table_name = f"backtest_{strategy_name}"
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    conn.close()
    logger.info(f"已保存回测结果: {strategy_name} -> {len(df)} 条记录")


def save_portfolio(state: dict):
    """保存持仓状态"""
    conn = get_connection()
    import json
    save_state = dict(state)
    save_state["holdings"] = json.dumps(state.get("holdings", {}), ensure_ascii=False)
    pd.DataFrame([save_state]).to_sql("portfolio", conn, if_exists="replace", index=False)
    conn.close()


def load_portfolio() -> dict:
    """加载持仓状态"""
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT * FROM portfolio", conn)
        if len(df) > 0:
            import json
            state = df.iloc[0].to_dict()
            state["holdings"] = json.loads(state.get("holdings", "{}"))
            return state
    except Exception:
        pass
    finally:
        conn.close()
    return {"cash": 20000.0, "holdings": {}, "total_value": 20000.0}


def save_stock_daily(df: pd.DataFrame, symbol: str):
    """将个股日线数据追加存入 SQLite（增量，去重）"""
    conn = get_connection()
    table_name = f"stock_{symbol}"

    existing = pd.DataFrame()
    try:
        existing = pd.read_sql(f"SELECT date FROM {table_name}", conn)
    except Exception:
        pass

    if not existing.empty:
        existing_dates = set(pd.to_datetime(existing["date"]).dt.strftime("%Y-%m-%d"))
        df = df.copy()
        df["date_str"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
        new_rows = df[~df["date_str"].isin(existing_dates)]
        new_rows = new_rows.drop(columns=["date_str"])
        if not new_rows.empty:
            new_rows.to_sql(table_name, conn, if_exists="append", index=False)
            logger.info(f"增量追加 {symbol}: +{len(new_rows)} 条")
        else:
            logger.info(f"{symbol} 数据已是最新")
    else:
        df.to_sql(table_name, conn, if_exists="replace", index=False)
        logger.info(f"已保存 {symbol} 日线数据: {len(df)} 条 -> {table_name}")

    conn.close()


def load_stock_daily(symbol: str) -> pd.DataFrame:
    """从 SQLite 加载个股日线数据"""
    conn = get_connection()
    table_name = f"stock_{symbol}"
    try:
        df = pd.read_sql(f"SELECT * FROM {table_name}", conn)
        df["date"] = pd.to_datetime(df["date"])
        return df
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def list_cached_stocks() -> list:
    """列出本地已缓存日线数据的股票代码"""
    conn = get_connection()
    try:
        tables = pd.read_sql("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stock_%'", conn)
        return [t.replace("stock_", "") for t in tables["name"].tolist()]
    except Exception:
        return []
    finally:
        conn.close()
