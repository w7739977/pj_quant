"""
Tushare 估值数据补全 — 通过日期批量获取全市场基本面数据

优势: 0.3秒/日期 × 1500交易日 ≈ 7.5分钟补完全部4417只股票的:
  pe_ttm, pb, ps_ttm, pcf_ncf_ttm, turnover_rate, volume_ratio

流程:
  1. 从 Tushare 获取交易日历
  2. 按日期批量获取 daily_basic (全市场)
  3. 保存为 Parquet 文件 (data/fundamentals/)
  4. 合并更新到 SQLite

用法:
  python3 -c "from data.tushare_fundamentals import run; run()"
  python3 -c "from data.tushare_fundamentals import run; run(limit=10)"  # 测试
"""

import os
import time
import logging
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime

logger = logging.getLogger(__name__)

TUSHARE_TOKEN = "ffdc605eabf943817596e0c3d68f5fbe5ed9e9cbe0af65d22313ed27"
PARQUET_DIR = "data/fundamentals_parquet"
DB_PATH = "data/quant.db"

FUND_FIELDS = [
    "ts_code", "trade_date", "close",
    "pe_ttm", "pb", "ps_ttm",
    "turnover_rate", "volume_ratio",
    "total_mv", "circ_mv",
]


def _init_tushare():
    """初始化 Tushare API"""
    import tushare as ts
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def fetch_trading_calendar(pro, start="20200101", end=None):
    """获取交易日历"""
    if end is None:
        end = datetime.now().strftime("%Y%m%d")
    df = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    return sorted(df["cal_date"].tolist())


def fetch_by_date(pro, trade_dates, limit=0):
    """按日期批量获取全市场基本面数据，保存为 Parquet"""
    os.makedirs(PARQUET_DIR, exist_ok=True)

    total = len(trade_dates)
    if limit > 0:
        total = min(limit, total)
        trade_dates = trade_dates[:total]

    success = 0
    fail = 0
    t0 = time.time()

    for i, date in enumerate(trade_dates):
        parquet_path = os.path.join(PARQUET_DIR, f"{date}.parquet")

        # 跳过已下载
        if os.path.exists(parquet_path):
            success += 1
            continue

        try:
            df = pro.daily_basic(
                trade_date=date,
                fields=",".join(FUND_FIELDS),
            )
            if df is not None and len(df) > 0:
                # 过滤只保留沪深A股 (sz/sh, 排除北交所BJ)
                df = df[~df["ts_code"].str.endswith(".BJ")]
                df.to_parquet(parquet_path, index=False)
                success += 1
            else:
                fail += 1
        except Exception as e:
            fail += 1
            logger.debug(f"{date}: {e}")

        # 进度报告
        if (i + 1) % 50 == 0 or (i + 1) == total:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total - i - 1) / rate / 60 if rate > 0 else 0
            print(f"  [{i+1}/{total}] ok={success} fail={fail} "
                  f"speed={rate:.1f}/s eta~{eta:.0f}min")

        # 限速: 200次/min → 每次间隔 0.35s
        time.sleep(0.35)

    elapsed = time.time() - t0
    print(f"\n下载完成: ok={success} fail={fail} 耗时={elapsed/60:.1f}min")
    return success, fail


def _ts_code_to_local(ts_code):
    """000001.SZ → 000001"""
    return ts_code.split(".")[0]


def import_to_sqlite(limit=0):
    """从 Parquet 文件导入到 SQLite — 按股票批量 UPDATE"""
    parquet_files = sorted(
        f for f in os.listdir(PARQUET_DIR) if f.endswith(".parquet")
    )
    if limit > 0:
        parquet_files = parquet_files[:limit]

    if not parquet_files:
        print("没有 Parquet 文件需要导入")
        return 0

    # Step 1: 合并所有 Parquet 到一个大 DataFrame
    print(f"  合并 {len(parquet_files)} 个 Parquet 文件...")
    dfs = []
    for fname in parquet_files:
        fpath = os.path.join(PARQUET_DIR, fname)
        dfs.append(pd.read_parquet(fpath))
    all_df = pd.concat(dfs, ignore_index=True)
    all_df["code"] = all_df["ts_code"].str.split(".").str[0]
    # 转换 trade_date 为日期字符串
    all_df["date_str"] = all_df["trade_date"].astype(str).str[:4] + "-" + \
                         all_df["trade_date"].astype(str).str[4:6] + "-" + \
                         all_df["trade_date"].astype(str).str[6:8]
    print(f"  合并完成: {len(all_df)} 条记录")

    # Step 2: 获取本地已有表名
    conn = sqlite3.connect(DB_PATH)
    existing_tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stock_%'"
        ).fetchall()
    }

    UPDATE_COLS = ["pe_ttm", "pb", "ps_ttm", "turnover_rate", "volume_ratio"]
    updated_stocks = set()
    total_stocks = all_df["code"].nunique()
    t0 = time.time()

    # Step 3: 按股票分组批量更新
    for i, (code, group) in enumerate(all_df.groupby("code")):
        table = f"stock_{code}"
        if table not in existing_tables:
            continue

        # 确保列存在
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col in UPDATE_COLS:
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
        conn.commit()

        # 读取本地日期映射 (rowid → date_str)
        local = pd.read_sql(f"SELECT rowid, date FROM {table}", conn)
        local["date_str"] = local["date"].astype(str).str[:10]

        # 合并
        merged = local.merge(group[["date_str"] + UPDATE_COLS], on="date_str", how="inner")

        if merged.empty:
            continue

        # 批量 UPDATE
        set_clause = ", ".join(f"{col}=?" for col in UPDATE_COLS)
        updates = []
        for _, row in merged.iterrows():
            vals = tuple(
                None if pd.isna(row[col]) else float(row[col])
                for col in UPDATE_COLS
            ) + (int(row["rowid"]),)
            updates.append(vals)

        conn.executemany(f"UPDATE {table} SET {set_clause} WHERE rowid=?", updates)
        conn.commit()
        updated_stocks.add(code)

        if (i + 1) % 500 == 0 or (i + 1) == total_stocks:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (total_stocks - i - 1) / rate / 60 if rate > 0 else 0
            print(f"  入库 [{i+1}/{total_stocks}] 已更新 {len(updated_stocks)} 只 "
                  f"eta~{eta:.0f}min")

    conn.close()
    elapsed = time.time() - t0
    print(f"\n入库完成: {len(updated_stocks)} 只股票已更新, 耗时={elapsed/60:.1f}min")
    return len(updated_stocks)


def run(limit=0):
    """一键运行: 下载 Parquet → 导入 SQLite"""
    print("=" * 60)
    print("Tushare 估值数据补全")
    print("=" * 60)

    pro = _init_tushare()

    # Step 1: 获取交易日历
    print("\n[1/3] 获取交易日历...")
    dates = fetch_trading_calendar(pro)
    print(f"  {len(dates)} 个交易日 ({dates[0]} ~ {dates[-1]})")

    # Step 2: 按日期下载
    print(f"\n[2/3] 按日期批量下载基本面数据...")
    fetch_by_date(pro, dates, limit=limit)

    # Step 3: 导入 SQLite
    print(f"\n[3/3] 导入 SQLite...")
    count = import_to_sqlite(limit=limit)

    print(f"\n{'='*60}")
    print(f"完成! 共更新 {count} 只股票的估值数据")
    print(f"{'='*60}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    import sys
    _limit = 0
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        _limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 10
    run(limit=_limit)
