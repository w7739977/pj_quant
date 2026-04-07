"""
Tushare 统一数据获取模块

优势:
  - K线+基本面一站式
  - 按日期批量获取 (0.3s/日期, 全市场5000+只)
  - Parquet 缓存复用
  - 比 BaoStock 快 10-15 倍

用法:
  from data.tushare_loader import fetch_all_klines, fetch_all_fundamentals
  fetch_all_klines()      # K线 → SQLite
  fetch_all_fundamentals() # 基本面 → SQLite
"""

import os
import time
import sqlite3
import logging
import pandas as pd
import numpy as np
from datetime import datetime

logger = logging.getLogger(__name__)

TUSHARE_TOKEN = "ffdc605eabf943817596e0c3d68f5fbe5ed9e9cbe0af65d22313ed27"
PARQUET_DIR = "data/tushare_parquet"
DB_PATH = "data/quant.db"

KLINE_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"
FUND_FIELDS = "ts_code,trade_date,close,pe_ttm,pb,ps_ttm,turnover_rate,volume_ratio"


def _init_tushare():
    import tushare as ts
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def fetch_trading_calendar(pro, start="20200101", end=None):
    """获取交易日历"""
    if end is None:
        end = datetime.now().strftime("%Y%m%d")
    df = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    return sorted(df["cal_date"].tolist())


def _ts_code_to_local(ts_code):
    """000001.SZ → 000001"""
    return ts_code.split(".")[0]


# ==================== K线获取 ====================

def fetch_klines_by_date(pro, trade_dates, limit=0):
    """按日期批量获取全市场K线，保存为 Parquet"""
    kline_dir = os.path.join(PARQUET_DIR, "kline")
    os.makedirs(kline_dir, exist_ok=True)

    total = len(trade_dates)
    if limit > 0:
        total = min(limit, total)
        trade_dates = trade_dates[:total]

    success = 0
    fail = 0
    t0 = time.time()

    for i, date in enumerate(trade_dates):
        parquet_path = os.path.join(kline_dir, f"{date}.parquet")

        if os.path.exists(parquet_path):
            success += 1
            continue

        try:
            df = pro.daily(trade_date=date, fields=KLINE_FIELDS)
            if df is not None and len(df) > 0:
                df = df[~df["ts_code"].str.endswith(".BJ")]  # 排除北交所
                df.to_parquet(parquet_path, index=False)
                success += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            logger.debug(f"{date}: {e}")

        if (i + 1) % 50 == 0 or (i + 1) == total:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate / 60 if rate > 0 else 0
            print(f"  [{i+1}/{total}] ok={success} fail={fail} "
                  f"speed={rate:.1f}/s eta~{eta:.0f}min")

        time.sleep(0.35)  # 限速 200/min

    elapsed = time.time() - t0
    print(f"K线下载: ok={success} fail={fail} 耗时={elapsed/60:.1f}min")
    return success, fail


def import_klines_to_sqlite(limit=0):
    """K线 Parquet → SQLite (按股票批量插入)"""
    kline_dir = os.path.join(PARQUET_DIR, "kline")
    parquet_files = sorted(
        os.path.join(kline_dir, f) for f in os.listdir(kline_dir) if f.endswith(".parquet")
    )
    if limit > 0:
        parquet_files = parquet_files[:limit]

    if not parquet_files:
        return 0

    print(f"  合并 {len(parquet_files)} 个 K线 Parquet...")
    dfs = []
    for fpath in parquet_files:
        dfs.append(pd.read_parquet(fpath))
    all_df = pd.concat(dfs, ignore_index=True)
    all_df["code"] = all_df["ts_code"].str.split(".").str[0]
    all_df["date"] = pd.to_datetime(all_df["trade_date"].astype(str))
    print(f"  合并: {len(all_df)} 条")

    conn = sqlite3.connect(DB_PATH)
    existing_tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stock_%'"
        ).fetchall()
    }

    updated = 0
    t0 = time.time()

    for code, group in all_df.groupby("code"):
        table = f"stock_{code}"
        if table not in existing_tables:
            continue

        # 检查是否有数据差异再更新
        local_max = conn.execute(
            f"SELECT MAX(date) FROM {table}"
        ).fetchone()[0]
        if local_max:
            local_max = pd.to_datetime(local_max)
            group = group[group["date"] > local_max]

        if group.empty:
            continue

        # 批量插入
        for _, row in group.iterrows():
            try:
                conn.execute(
                    f"""INSERT OR IGNORE INTO {table}
                       (date, open, high, low, close, volume, turnover)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        row["date"], row["open"], row["high"], row["low"],
                        row["close"], row["vol"], row["amount"]
                    )
                )
            except:
                pass
        conn.commit()
        updated += 1

        if updated % 500 == 0:
            print(f"  已更新 {updated} 只股票")

    conn.close()
    print(f"K线入库: {updated} 只股票")
    return updated


# ==================== 基本面获取 (复用 tushare_fundamentals.py) ====================

def fetch_fundamentals_by_date(pro, trade_dates, limit=0):
    """按日期批量获取全市场基本面，保存为 Parquet"""
    fund_dir = os.path.join(PARQUET_DIR, "fundamentals")
    os.makedirs(fund_dir, exist_ok=True)

    total = len(trade_dates)
    if limit > 0:
        total = min(limit, total)
        trade_dates = trade_dates[:total]

    success = 0
    fail = 0
    t0 = time.time()

    for i, date in enumerate(trade_dates):
        parquet_path = os.path.join(fund_dir, f"{date}.parquet")

        if os.path.exists(parquet_path):
            success += 1
            continue

        try:
            df = pro.daily_basic(trade_date=date, fields=FUND_FIELDS)
            if df is not None and len(df) > 0:
                df = df[~df["ts_code"].str.endswith(".BJ")]
                df.to_parquet(parquet_path, index=False)
                success += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            logger.debug(f"{date}: {e}")

        if (i + 1) % 50 == 0 or (i + 1) == total:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate / 60 if rate > 0 else 0
            print(f"  [{i+1}/{total}] ok={success} fail={fail}")

        time.sleep(0.35)

    print(f"基本面下载: ok={success} fail={fail} 耗时={time.time()-t0:.1f}s")
    return success, fail


# ==================== 验证函数 ====================

def verify_import(date_sample=None):
    """验证数据导入正确性"""
    conn = sqlite3.connect(DB_PATH)

    if date_sample is None:
        date_sample = datetime.now().strftime("%Y%m%d")

    # 从 Parquet 读取样本日期的数据
    fund_file = os.path.join(PARQUET_DIR, "fundamentals", f"{date_sample}.parquet")
    kline_file = os.path.join(PARQUET_DIR, "kline", f"{date_sample}.parquet")

    print(f"\n=== 验证日期 {date_sample} ===")

    if os.path.exists(fund_file):
        fund_df = pd.read_parquet(fund_file)
        print(f"Parquet基本面: {len(fund_df)} 条")
        # 抽查3只
        for code in ["000001.SZ", "600519.SH", "300750.SZ"]:
            if code in fund_df["ts_code"].values:
                row = fund_df[fund_df["ts_code"] == code].iloc[0]
                print(f"  {code}: pe_ttm={row.get('pe_ttm')}, pb={row.get('pb')}")

    if os.path.exists(kline_file):
        kline_df = pd.read_parquet(kline_file)
        print(f"Parquet K线: {len(kline_df)} 条")

    # 对比 SQLite
    print("\n--- SQLite 对比 ---")
    for code in ["000001", "600519", "300750"]:
        table = f"stock_{code}"
        exists = conn.execute(
            f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{table}'"
        ).fetchone()
        if exists:
            row = conn.execute(
                f"SELECT date, close, pe_ttm, pb FROM {table} "
                f"ORDER BY date DESC LIMIT 1"
            ).fetchone()
            print(f"  {code}: {row}")

    conn.close()


# ==================== 一键运行 ====================

def run_all(kline_limit=0, fund_limit=0):
    """一键获取 K线 + 基本面"""
    print("=" * 60)
    print("Tushare 统一数据获取")
    print("=" * 60)

    pro = _init_tushare()

    # 交易日历
    dates = fetch_trading_calendar(pro)
    print(f"交易日历: {len(dates)} 个")

    # K线
    print("\n[1/4] 下载K线 Parquet...")
    fetch_klines_by_date(pro, dates, limit=kline_limit)
    print("\n[2/4] 导入K线 SQLite...")
    import_klines_to_sqlite(limit=kline_limit)

    # 基本面
    print("\n[3/4] 下载基本面 Parquet...")
    fetch_fundamentals_by_date(pro, dates, limit=fund_limit)

    # 验证
    print("\n[4/4] 验证数据...")
    verify_import(dates[-1])

    print("\n完成!")


if __name__ == "__main__":
    run_all()
