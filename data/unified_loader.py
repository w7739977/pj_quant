"""
统一数据获取入口 — 本地优先架构

数据优先级:
  1. 本地 SQLite (最快，已有完整数据)
  2. 本地 Parquet (Tushare 缓存，可复用)
  3. Tushare API (网络兜底)

用法:
  from data.unified_loader import get_stock_data, get_stock_pool

  # 获取单只股票（自动优先本地）
  df = get_stock_data("000001", start="2020-01-01")

  # 获取股票池（优先本地列表）
  pool = get_stock_pool(min_cap=5e8, max_cap=5e9)
"""

import os
import sqlite3
import pandas as pd
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

DB_PATH = "data/quant.db"
PARQUET_DIR = "data/tushare_parquet"

# ==================== 1. 股票池获取 (本地优先) ====================

def get_stock_pool(min_cap: float = 5e8, max_cap: float = 5e9) -> pd.DataFrame:
    """
    获取股票池，本地优先

    优先级:
      1. 本地股票列表 (list_cached_stocks) → 4417只 ✓
      2. 实时市值补充 (可选，用腾讯批量)
    """
    from data.storage import list_cached_stocks

    cached = list_cached_stocks()
    if len(cached) > 100:
        # 本地有完整列表
        stock_df = pd.DataFrame({"code": cached})

        # 可选：补充实时市值
        try:
            from data.fetcher import fetch_realtime_tencent_batch
            all_rt = []
            for i in range(0, len(cached), 100):
                batch = cached[i:i+100]
                rt = fetch_realtime_tencent_batch(batch)
                if not rt.empty:
                    all_rt.append(rt)

            if all_rt:
                rt_df = pd.concat(all_rt, ignore_index=True)
                rt_df["market_cap"] = pd.to_numeric(
                    rt_df.get("total_market_cap", 0), errors="coerce"
                ) * 1e8  # 亿→元
                stock_df = stock_df.merge(
                    rt_df[["code", "market_cap"]], on="code", how="left"
                )

                # 市值过滤
                stock_df = stock_df[
                    (stock_df["market_cap"] >= min_cap) &
                    (stock_df["market_cap"] <= max_cap)
                ]
                logger.info(f"本地+腾讯: {len(stock_df)} 只")
                return stock_df
        except Exception as e:
            logger.warning(f"腾讯市值获取失败: {e}")

        # 无市值过滤，返回全部
        logger.info(f"本地股票池: {len(stock_df)} 只")
        return stock_df

    # 本地无数据 → Tushare兜底
    logger.warning("本地无股票池，使用Tushare获取...")
    return _get_stock_pool_tushare(min_cap, max_cap)


def _get_stock_pool_tushare(min_cap, max_cap):
    """Tushare 兜底：获取股票池"""
    import tushare as ts
    ts.set_token("ffdc605eabf943817596e0c3d68f5fbe5ed9e9cbe0af65d22313ed27")
    pro = ts.pro_api()

    df = pro.stock_basic(
        exchange="", list_status="L",
        fields="ts_code,symbol,name,area,industry,list_date"
    )
    df["code"] = df["ts_code"].str.split(".").str[0]

    # 过滤
    df = df[~df["name"].str.contains("ST|退|N", na=False)]
    df = df[~df["code"].str.startswith(("8", "688"))]
    df = df[df["list_date"].notna()]

    logger.info(f"Tushare股票池: {len(df)} 只")
    return df[["code", "name"]].reset_index(drop=True)


# ==================== 2. 个股数据获取 (本地优先) ====================

def get_stock_data(symbol: str, start_date: str, end_date: str = None) -> pd.DataFrame:
    """
    获取个股数据，本地优先

    优先级:
      1. 本地 SQLite (已有4417只完整K线+估值) ✓
      2. 本地 Parquet (Tushare缓存)
      3. Tushare API (网络兜底)

    Parameters
    ----------
    symbol : str  股票代码，如 "000001"
    start_date : str  开始日期
    end_date : str  结束日期

    Returns
    -------
    DataFrame: [date, open, high, low, close, volume, turnover, pct_chg,
                pe_ttm, pb, ps_ttm, turnover_rate, volume_ratio]
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # ===== 优先级1: 本地 SQLite =====
    from data.storage import load_stock_daily
    df = load_stock_daily(symbol)

    if not df.empty:
        # 过滤日期范围
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        df = df[(df["date"] >= start_dt) & (df["date"] <= end_dt)]

        if not df.empty:
            # 检查数据完整性
            expected_cols = ["open", "high", "low", "close", "volume"]
            has_kline = all(c in df.columns for c in expected_cols)

            if has_kline:
                logger.debug(f"{symbol}: 本地SQLite, {len(df)}条")
                return df.reset_index(drop=True)

    # ===== 优先级2: 本地 Parquet =====
    df = _load_from_parquet(symbol, start_date, end_date)
    if not df.empty:
        logger.debug(f"{symbol}: 本地Parquet, {len(df)}条")
        return df

    # ===== 优先级3: Tushare API (兜底) =====
    logger.warning(f"{symbol}: 本地无数据，尝试Tushare...")
    return _fetch_from_tushare(symbol, start_date, end_date)


def _load_from_parquet(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """从本地 Parquet 缓存读取"""
    # 检查 K线 Parquet
    kline_dir = os.path.join(PARQUET_DIR, "kline")
    fund_dir = os.path.join(PARQUET_DIR, "fundamentals")

    if not os.path.exists(kline_dir):
        return pd.DataFrame()

    # 读取相关日期的 Parquet 文件
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date)

    kline_files = []
    for fname in os.listdir(kline_dir):
        if fname.endswith(".parquet"):
            date_str = fname.replace(".parquet", "")
            fdate = pd.to_datetime(date_str)
            if start_dt <= fdate <= end_dt:
                kline_files.append(os.path.join(kline_dir, fname))

    if not kline_files:
        return pd.DataFrame()

    # 合并 K线
    dfs = []
    for fpath in sorted(kline_files):
        df = pd.read_parquet(fpath)
        df["code"] = df["ts_code"].str.split(".").str[0]
        dfs.append(df)

    if dfs:
        kline_df = pd.concat(dfs, ignore_index=True)
        kline_df = kline_df[kline_df["code"] == symbol]

        if not kline_df.empty:
            # 合并基本面
            fund_dfs = []
            for fpath in kline_files:
                fund_path = os.path.join(
                    fund_dir, os.path.basename(fpath)
                )
                if os.path.exists(fund_path):
                    fund_df = pd.read_parquet(fund_path)
                    fund_df["code"] = fund_df["ts_code"].str.split(".").str[0]
                    fund_dfs.append(fund_df)

            if fund_dfs:
                fund_df = pd.concat(fund_dfs, ignore_index=True)
                fund_df = fund_df[fund_df["code"] == symbol]

                # 合并
                merged = kline_df.merge(
                    fund_df[["trade_date", "pe_ttm", "pb", "ps_ttm",
                            "turnover_rate", "volume_ratio"]],
                    on="trade_date",
                    how="left"
                )

                # 格式化
                merged["date"] = pd.to_datetime(merged["trade_date"])
                return merged[["date", "open", "high", "low", "close",
                               "vol", "amount", "pe_ttm", "pb", "ps_ttm",
                               "turnover_rate", "volume_ratio"]]

    return pd.DataFrame()


def _fetch_from_tushare(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Tushare API 兜底获取"""
    import tushare as ts
    ts.set_token("ffdc605eabf943817596e0c3d68f5fbe5ed9e9cbe0af65d22313ed27")
    pro = ts.pro_api()

    ts_code = f"{symbol}.SZ" if symbol.startswith(("0", "3")) else f"{symbol}.SH"

    # K线
    kline = pro.daily(
        ts_code=ts_code,
        start_date=start_date.replace("-", ""),
        end_date=end_date.replace("-", "")
    )

    if kline is None or kline.empty:
        return pd.DataFrame()

    kline["date"] = pd.to_datetime(kline["trade_date"])

    # 基本面
    fund = pro.daily_basic(
        ts_code=ts_code,
        start_date=start_date.replace("-", ""),
        end_date=end_date.replace("-", ""),
        fields="ts_code,trade_date,pe_ttm,pb,ps_ttm,turnover_rate,volume_ratio"
    )

    if fund is not None and not fund.empty:
        fund["date"] = pd.to_datetime(fund["trade_date"])
        kline = kline.merge(
            fund[["date", "pe_ttm", "pb", "ps_ttm", "turnover_rate", "volume_ratio"]],
            on="date",
            how="left"
        )

    return kline.reset_index(drop=True)


# ==================== 3. 批量数据更新 (增量) ====================

def update_latest_data():
    """
    增量更新最新数据

    策略:
      1. 检查本地最新日期
      2. 从 Tushare 拉取增量数据
      3. 追加到 SQLite
    """
    from data.storage import list_cached_stocks

    conn = sqlite3.connect(DB_PATH)
    cached = list_cached_stocks()

    # 检查最新日期
    sample_code = cached[0] if cached else "000001"
    latest = conn.execute(
        f"SELECT MAX(date) FROM stock_{sample_code}"
    ).fetchone()[0]
    conn.close()

    if latest:
        latest_str = pd.to_datetime(latest).strftime("%Y%m%d")
        today = datetime.now().strftime("%Y%m%d")

        if latest_str >= today:
            logger.info("数据已是最新")
            return

        logger.info(f"增量更新: {latest_str} → {today}")
        # 调用 Tushare 拉取增量...
        # TODO: 实现增量更新逻辑
    else:
        logger.info("本地无数据，需要全量更新")


if __name__ == "__main__":
    # 测试
    df = get_stock_data("000001", "2026-03-01")
    print(df.head())
    print(f"\n列: {df.columns.tolist()}")
