"""
Tushare 全市场日线数据批量获取 — 按日期批量拉取

优势: 按日期获取全市场日线，0.35s/日期 × ~1500交易日 ≈ 9分钟
对比 BaoStock 逐只拉取需要 3-4 小时

流程:
  1. 从 Tushare 获取交易日历
  2. 按日期批量获取 daily (全市场 OHLCV)
  3. 保存为 Parquet 文件 (data/daily_parquet/)
  4. 按股票拆分导入 SQLite (stock_XXXXXX 表)

用法:
  python3 -c "from data.tushare_daily import run; run()"
  python3 -c "from data.tushare_daily import run; run(limit=10)"  # 测试10天
"""

import os
import time
import logging
import sqlite3
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "ffdc605eabf943817596e0c3d68f5fbe5ed9e9cbe0af65d22313ed27")
PARQUET_DIR = "data/daily_parquet"
DB_PATH = "data/quant.db"

DAILY_FIELDS = [
    "ts_code", "trade_date", "open", "high", "low", "close",
    "vol", "amount", "pct_chg",
]


def _init_tushare():
    import tushare as ts
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def fetch_trading_calendar(pro, start="20200101", end=None):
    if end is None:
        end = datetime.now().strftime("%Y%m%d")
    df = pro.trade_cal(exchange="SSE", start_date=start, end_date=end, is_open="1")
    return sorted(df["cal_date"].tolist())


def fetch_daily_by_date(pro, trade_dates, limit=0):
    """按日期批量获取全市场日线数据，保存为 Parquet"""
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

        if os.path.exists(parquet_path):
            success += 1
            continue

        try:
            df = pro.daily(trade_date=date)
            if df is not None and len(df) > 0:
                # 只保留需要的列
                available_cols = [c for c in DAILY_FIELDS if c in df.columns]
                df = df[available_cols]
                # 排除北交所
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
            print(f"  [{i+1}/{total}] ok={success} fail={fail} "
                  f"speed={rate:.1f}/s eta~{eta:.0f}min")

        time.sleep(0.35)

    elapsed = time.time() - t0
    print(f"\n下载完成: ok={success} fail={fail} 耗时={elapsed/60:.1f}min")
    return success, fail


def _process_parquet_batch(conn, batch_files, written_tables):
    """处理一批 Parquet 文件，按股票拆分写入 SQLite"""
    dfs = []
    for fname in batch_files:
        fpath = os.path.join(PARQUET_DIR, fname)
        dfs.append(pd.read_parquet(fpath))
    if not dfs:
        return 0
    batch_df = pd.concat(dfs, ignore_index=True)
    batch_df["code"] = batch_df["ts_code"].str.split(".").str[0]

    col_map = {"vol": "volume", "amount": "turnover"}
    batch_df = batch_df.rename(columns=col_map)
    batch_df["date"] = pd.to_datetime(batch_df["trade_date"], format="%Y%m%d")

    # 排除科创板
    batch_df = batch_df[~batch_df["code"].str.startswith("688")]

    final_cols = ["date", "open", "high", "low", "close", "volume", "turnover", "pct_chg"]
    for c in ["open", "high", "low", "close", "volume", "turnover", "pct_chg"]:
        batch_df[c] = pd.to_numeric(batch_df[c], errors="coerce")
        nan_rate = batch_df[c].isna().mean()
        if nan_rate > 0.1:
            logger.warning(f"列 {c} NaN 率 {nan_rate:.0%}，可能数据源异常")

    written = 0
    for code, group in batch_df.groupby("code"):
        table = f"stock_{code}"
        stock_df = group[[c for c in final_cols if c in group.columns]].sort_values("date").reset_index(drop=True)

        if table in written_tables:
            stock_df.to_sql(table, conn, if_exists="append", index=False)
        else:
            stock_df.to_sql(table, conn, if_exists="replace", index=False)
            written_tables.add(table)
        written += 1

    return written


def import_to_sqlite(limit=0):
    """从 Parquet 文件按股票拆分导入 SQLite（分批处理，避免内存溢出）"""
    parquet_files = sorted(
        f for f in os.listdir(PARQUET_DIR) if f.endswith(".parquet")
    )
    if limit > 0:
        parquet_files = parquet_files[:limit]

    if not parquet_files:
        print("没有 Parquet 文件需要导入")
        return 0

    conn = sqlite3.connect(DB_PATH)
    try:
        # 清空旧 stock_* 表（确保干净导入）
        old_tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stock_%'"
            ).fetchall()
        ]
        if old_tables:
            print(f"  清空 {len(old_tables)} 个旧 stock 表...")
            for t in old_tables:
                conn.execute(f"DROP TABLE IF EXISTS {t}")
            conn.commit()

        written_tables = set()
        BATCH_SIZE = 100  # 每批100个日期文件 (~48万行)
        total_files = len(parquet_files)
        t0 = time.time()

        for batch_start in range(0, total_files, BATCH_SIZE):
            batch = parquet_files[batch_start:batch_start + BATCH_SIZE]
            _process_parquet_batch(conn, batch, written_tables)
            conn.commit()

            done = min(batch_start + BATCH_SIZE, total_files)
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total_files - done) / rate / 60 if rate > 0 else 0
            print(f"  入库 [{done}/{total_files} 文件] {len(written_tables)} 只股票 eta~{eta:.0f}min")

        elapsed = time.time() - t0
        print(f"\n入库完成: {len(written_tables)} 只股票, 耗时={elapsed/60:.1f}min")
        return len(written_tables)
    finally:
        conn.close()


def get_last_trade_date_in_db(db_path=DB_PATH) -> Optional[str]:
    """
    查询 SQLite 中所有 stock_* 表的最大日期，返回全局最新日期。

    随机抽样 10 只股票取 MAX(date)，避免全表扫描。

    Returns
    -------
    str | None
        YYYYMMDD 格式字符串（如 "20260407"），空库返回 None
    """
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stock_%'"
            ).fetchall()
        ]
        if not tables:
            return None

        # 随机抽样 10 只取 MAX(date)
        import random
        sample = random.sample(tables, min(10, len(tables)))

        max_date = None
        for table in sample:
            try:
                row = conn.execute(f"SELECT MAX(date) FROM {table}").fetchone()
                if row and row[0]:
                    d = str(row[0])[:10].replace("-", "")
                    if max_date is None or d > max_date:
                        max_date = d
            except Exception:
                continue

        return max_date
    finally:
        conn.close()


def import_to_sqlite_incremental(new_dates: list) -> int:
    """
    增量导入: 只读取 new_dates 对应的 Parquet 文件，追加到 SQLite。

    不会 DROP 任何表。对已有表用 DELETE 去重后再 append。

    Parameters
    ----------
    new_dates : list[str]
        需要导入的日期列表，YYYYMMDD 格式 (如 ["20260408"])

    Returns
    -------
    int
        更新的股票数量
    """
    # 只读取 new_dates 对应的 Parquet
    parquet_files = [f"{d}.parquet" for d in new_dates]
    parquet_files = [f for f in parquet_files if os.path.exists(os.path.join(PARQUET_DIR, f))]

    if not parquet_files:
        print("  没有新的 Parquet 文件需要导入")
        return 0

    print(f"  增量导入 {len(parquet_files)} 个日期文件...")

    conn = sqlite3.connect(DB_PATH)
    try:
        # 去重: 删除已有表中 >= 最早新日期的数据
        min_date_yyyymmdd = min(new_dates)
        min_date_str = f"{min_date_yyyymmdd[:4]}-{min_date_yyyymmdd[4:6]}-{min_date_yyyymmdd[6:8]}"

        existing_tables = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stock_%'"
            ).fetchall()
        }

        # 用 _process_parquet_batch 处理，但禁止 replace
        dfs = []
        for fname in parquet_files:
            fpath = os.path.join(PARQUET_DIR, fname)
            dfs.append(pd.read_parquet(fpath))

        if not dfs:
            return 0

        batch_df = pd.concat(dfs, ignore_index=True)
        batch_df["code"] = batch_df["ts_code"].str.split(".").str[0]

        col_map = {"vol": "volume", "amount": "turnover"}
        batch_df = batch_df.rename(columns=col_map)
        batch_df["date"] = pd.to_datetime(batch_df["trade_date"], format="%Y%m%d")

        # 排除科创板
        batch_df = batch_df[~batch_df["code"].str.startswith("688")]

        final_cols = ["date", "open", "high", "low", "close", "volume", "turnover", "pct_chg"]
        for c in ["open", "high", "low", "close", "volume", "turnover", "pct_chg"]:
            batch_df[c] = pd.to_numeric(batch_df[c], errors="coerce")
            nan_rate = batch_df[c].isna().mean()
            if nan_rate > 0.1:
                logger.warning(f"列 {c} NaN 率 {nan_rate:.0%}，可能数据源异常")

        written = 0
        for code, group in batch_df.groupby("code"):
            table = f"stock_{code}"
            stock_df = group[[c for c in final_cols if c in group.columns]].sort_values("date").reset_index(drop=True)

            if table in existing_tables:
                # 已有表: 先删除重复日期，再追加
                conn.execute(f"DELETE FROM {table} WHERE date >= ?", (min_date_str,))
                stock_df.to_sql(table, conn, if_exists="append", index=False)
            else:
                # 新表(IPO新股): 直接创建
                stock_df.to_sql(table, conn, if_exists="replace", index=False)
                existing_tables.add(table)
            written += 1

        conn.commit()
        print(f"  增量入库完成: {written} 只股票")
        return written
    finally:
        conn.close()


def run(limit=0, incremental=False):
    """
    一键运行: 下载日线数据 → 导入 SQLite

    Parameters
    ----------
    limit : int
        限制处理的交易日数量，0 表示全部
    incremental : bool
        True = 增量模式（只补齐新交易日），False = 全量模式（从 2020-01-01 重建）
    """
    mode_str = "增量更新" if incremental else "全量获取"
    print("=" * 60)
    print(f"Tushare 全市场日线数据 — {mode_str}")
    print("=" * 60)

    pro = _init_tushare()

    # Step 1: 获取交易日历
    print("\n[1/3] 获取交易日历...")
    dates = fetch_trading_calendar(pro)
    print(f"  {len(dates)} 个交易日 ({dates[0]} ~ {dates[-1]})")

    if incremental:
        # 增量模式: 只补齐 DB 最新日期之后的交易日
        last_date = get_last_trade_date_in_db()
        if last_date is None:
            print("  ⚠️ 本地无数据，自动切换为全量模式")
            incremental = False
        else:
            new_dates = [d for d in dates if d > last_date]
            if not new_dates:
                print(f"  已是最新 (DB最新: {last_date})，无需更新")
                print(f"\n{'='*60}")
                print(f"增量检查完成: 无新交易日")
                print(f"{'='*60}")
                return
            print(f"  增量模式: 从 {last_date} 之后补齐 {len(new_dates)} 个交易日")

            # Step 2: 下载新日期
            print(f"\n[2/4] 下载 {len(new_dates)} 个新交易日...")
            fetch_daily_by_date(pro, new_dates, limit=limit or 0)

            # Step 3: 增量导入 K 线
            print(f"\n[3/4] 增量导入 SQLite...")
            count = import_to_sqlite_incremental(new_dates)

            # Step 4: 链式补全基本面
            print(f"\n[4/4] 增量补全基本面数据...")
            try:
                from data.tushare_fundamentals import run as run_fundamentals
                run_fundamentals(incremental=True)
            except Exception as e:
                print(f"  ⚠️ 基本面增量补全失败: {e}")
                print(f"  可手动执行: python3 -c \"from data.tushare_fundamentals import run; run(incremental=True)\"")

            print(f"\n{'='*60}")
            print(f"增量更新完成! 补齐 {len(new_dates)} 个交易日, {count} 只股票")
            print(f"{'='*60}")
            return

    # 全量模式（原流程）
    # Step 2: 按日期下载
    print(f"\n[2/3] 按日期批量下载日线数据...")
    fetch_daily_by_date(pro, dates, limit=limit)

    # Step 3: 导入 SQLite
    print(f"\n[3/3] 导入 SQLite...")
    count = import_to_sqlite(limit=limit)

    # Step 4: 自动补全基本面数据
    print(f"\n[4/4] 补全基本面数据 (pe_ttm, pb, turnover_rate, volume_ratio)...")
    try:
        from data.tushare_fundamentals import run as run_fundamentals
        run_fundamentals(limit=limit)
    except Exception as e:
        print(f"  ⚠️ 基本面补全失败: {e}")
        print(f"  请手动执行: python3 -c \"from data.tushare_fundamentals import run; run()\"")

    print(f"\n{'='*60}")
    print(f"完成! 共入库 {count} 只股票 (K线 + 基本面)")
    print(f"{'='*60}")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    _limit = 0
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        _limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 10

    run(limit=_limit)
