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
import random
import pandas as pd
import numpy as np
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "ffdc605eabf943817596e0c3d68f5fbe5ed9e9cbe0af65d22313ed27")
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


def get_last_fundamental_date_in_db(db_path=DB_PATH) -> Optional[str]:
    """
    查询 SQLite 中基本面最新日期（pe_ttm 非空）。

    优先从高流动性锚点股查询，锚点不可用时回退随机抽样。

    Returns
    -------
    str | None
        YYYYMMDD 格式（如 "20260407"），无基本面数据返回 None
    """
    conn = sqlite3.connect(db_path)
    try:
        # 锚点股（高流动性，pe_ttm 覆盖率高）
        anchors = ["stock_000001", "stock_600519", "stock_600036", "stock_000858"]
        existing = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'stock_%'"
            ).fetchall()
        }

        tables_to_check = [t for t in anchors if t in existing]
        if not tables_to_check:
            # 回退: 随机抽 10 只
            all_stocks = list(existing)
            if not all_stocks:
                return None
            tables_to_check = random.sample(all_stocks, min(10, len(all_stocks)))

        max_date = None
        for table in tables_to_check:
            try:
                row = conn.execute(
                    f"SELECT MAX(date) FROM {table} WHERE pe_ttm IS NOT NULL"
                ).fetchone()
                if row and row[0]:
                    d = str(row[0])[:10].replace("-", "")
                    if max_date is None or d > max_date:
                        max_date = d
            except Exception:
                continue

        return max_date
    finally:
        conn.close()


def import_to_sqlite(limit=0, only_dates=None):
    """
    从 Parquet 文件导入到 SQLite — 按股票批量 UPDATE

    Parameters
    ----------
    limit : int
        限制处理的 Parquet 文件数，0 表示全部
    only_dates : list[str] | None
        增量模式：只处理指定日期（YYYYMMDD）的 Parquet。
        None 时处理全部（全量模式）。
    """
    if only_dates is not None:
        parquet_files = [f"{d}.parquet" for d in only_dates]
        parquet_files = [f for f in parquet_files
                         if os.path.exists(os.path.join(PARQUET_DIR, f))]
    else:
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

    UPDATE_COLS = ["pe_ttm", "pb", "ps_ttm", "turnover_rate", "volume_ratio", "total_mv"]
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

    # ===== 验证入库结果 =====
    # 增量模式跳过验证（数据量小），全量模式保留
    if only_dates is None:
        print("\n  验证入库结果...")
        _verify_import()

    # ===== M6: 刷新 latest_market_cap 汇总表 =====
    # 不影响主流程（避免下次 get_small_cap_stocks 跑 4400 次 SQL）
    try:
        from data.storage import refresh_latest_market_cap
        n = refresh_latest_market_cap()
        print(f"  汇总表 latest_market_cap 已刷新: {n} 只")
    except Exception as e:
        print(f"  汇总表刷新失败（不影响主流程）: {e}")

    print(f"\n入库完成: {len(updated_stocks)} 只股票已更新, 耗时={elapsed/60:.1f}min")
    return len(updated_stocks)


def _verify_import(sample_date=None):
    """验证入库数据正确性 (对比 Parquet 源文件)"""
    import os
    if sample_date is None:
        # 取中间某个日期验证
        files = sorted(f for f in os.listdir(PARQUET_DIR) if f.endswith(".parquet"))
        if not files:
            return
        sample_date = files[len(files)//2].replace(".parquet", "")

    parquet_path = os.path.join(PARQUET_DIR, f"{sample_date}.parquet")
    if not os.path.exists(parquet_path):
        return

    source_df = pd.read_parquet(parquet_path)
    source_df["code"] = source_df["ts_code"].str.split(".").str[0]

    # 抽查3只股票
    conn = sqlite3.connect(DB_PATH)
    test_codes = source_df["code"].head(3).tolist()

    for code in test_codes:
        table = f"stock_{code}"
        date_str = f"{sample_date[:4]}-{sample_date[4:6]}-{sample_date[6:8]}"

        row = source_df[source_df["code"] == code].iloc[0]
        local = conn.execute(
            f"SELECT pe_ttm, pb, ps_ttm, turnover_rate, volume_ratio FROM {table} "
            f"WHERE date(date) = ?", (date_str,)
        ).fetchone()

        if local:
            match = True
            for i, col in enumerate(["pe_ttm", "pb", "ps_ttm", "turnover_rate", "volume_ratio"]):
                src_val = row.get(col)
                local_val = local[i]
                if pd.notna(src_val) and pd.notna(local_val):
                    if abs(float(src_val) - float(local_val)) > 0.01:
                        match = False
            status = "✓" if match else "✗"
            print(f"    {code} {date_str}: {status}")
        else:
            print(f"    {code} {date_str}: ✗ (无记录)")

    conn.close()


def run(limit=0, incremental=False):
    """
    一键运行: 下载 Parquet → 导入 SQLite

    Parameters
    ----------
    limit : int
        限制处理的交易日数量，0 表示全部
    incremental : bool
        True = 增量模式（只补齐基本面最新日期之后的新交易日），
        False = 全量模式
    """
    mode_str = "增量更新" if incremental else "全量获取"
    print("=" * 60)
    print(f"Tushare 估值数据补全 — {mode_str}")
    print("=" * 60)

    pro = _init_tushare()

    # Step 1: 获取交易日历
    print("\n[1/3] 获取交易日历...")
    dates = fetch_trading_calendar(pro)
    print(f"  {len(dates)} 个交易日 ({dates[0]} ~ {dates[-1]})")

    if incremental:
        last_fund_date = get_last_fundamental_date_in_db()
        if last_fund_date is None:
            print("  ⚠️ 本地无基本面数据，自动切换为全量模式")
            incremental = False
        else:
            new_dates = [d for d in dates if d > last_fund_date]
            if not new_dates:
                print(f"  基本面已是最新 (最新: {last_fund_date})")
                print(f"\n{'='*60}")
                print(f"基本面增量检查完成: 无需更新")
                print(f"{'='*60}")
                return
            print(f"  基本面增量: 从 {last_fund_date} 之后补齐 {len(new_dates)} 个交易日")

            # Step 2: 下载新日期
            print(f"\n[2/3] 下载 {len(new_dates)} 个新交易日基本面数据...")
            fetch_by_date(pro, new_dates, limit=0)

            # Step 3: 增量导入
            print(f"\n[3/3] 增量导入 SQLite...")
            count = import_to_sqlite(only_dates=new_dates)

            print(f"\n{'='*60}")
            print(f"基本面增量完成! 补齐 {len(new_dates)} 个交易日, {count} 只股票")
            print(f"{'='*60}")
            return

    # 全量模式（原流程）
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
