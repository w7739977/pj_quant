"""
Tushare 财务指标 (fina_indicator) — PIT 数据，按公告日 (ann_date) 入库

字段:
  ann_date         公告日 ← 训练时必须用此日期，避免未来数据泄露
  end_date         财报截止日 (用于报告期识别)
  roe_yearly       年化 ROE (%)
  or_yoy           营收同比增速 (%)
  dt_eps_yoy       扣非 EPS 同比增速 (%)
  debt_to_assets   资产负债率 (%)
  netprofit_yoy    备用：净利润同比增速

更新策略:
  - 历史回填：按股票批量拉 5 年数据
  - 每月增量：每月 1 号 + 季报披露窗口（4/8/10 月）拉新数据
"""

import os
import time
import sqlite3
import logging
import pandas as pd
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)
DB_PATH = "data/quant.db"

# fina_indicator 接口要的字段（多取以备扩展）
FINA_FIELDS = [
    "ts_code", "ann_date", "end_date",
    "roe_yearly", "or_yoy", "dt_eps_yoy", "debt_to_assets",
    "netprofit_yoy", "op_yoy", "netprofit_margin",
]


def _init_tushare():
    import tushare as ts
    from data.tushare_fundamentals import TUSHARE_TOKEN
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def _init_table(conn):
    """财务指标表（PIT 数据，按 code+ann_date+end_date 主键）"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS financial_indicator (
            code TEXT,
            ann_date TEXT,
            end_date TEXT,
            roe_yearly REAL,
            or_yoy REAL,
            dt_eps_yoy REAL,
            debt_to_assets REAL,
            netprofit_yoy REAL,
            op_yoy REAL,
            netprofit_margin REAL,
            updated_at TEXT,
            PRIMARY KEY (code, ann_date, end_date)
        )
    """)
    # 关键索引：按 (code, ann_date) 查询时性能
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fin_code_ann ON financial_indicator(code, ann_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fin_ann ON financial_indicator(ann_date)")
    conn.commit()


def fetch_one_stock(pro, ts_code: str, start_date: str = "20200101") -> pd.DataFrame:
    """拉单股财务指标历史"""
    try:
        df = pro.fina_indicator(
            ts_code=ts_code,
            start_date=start_date,
            fields=",".join(FINA_FIELDS),
        )
        if df is None or df.empty:
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.warning(f"fina_indicator {ts_code}: {e}")
        return pd.DataFrame()


def save_batch(rows: list) -> int:
    """批量入库"""
    if not rows:
        return 0
    conn = sqlite3.connect(DB_PATH)
    try:
        _init_table(conn)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = [
            (r["code"], r["ann_date"], r["end_date"],
             r.get("roe_yearly"), r.get("or_yoy"),
             r.get("dt_eps_yoy"), r.get("debt_to_assets"),
             r.get("netprofit_yoy"), r.get("op_yoy"),
             r.get("netprofit_margin"),
             now)
            for r in rows
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO financial_indicator
            (code, ann_date, end_date, roe_yearly, or_yoy, dt_eps_yoy,
             debt_to_assets, netprofit_yoy, op_yoy, netprofit_margin, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            data,
        )
        conn.commit()
        return len(data)
    finally:
        conn.close()


def batch_fetch_all(start_date: str = "20200101", limit: int = 0):
    """
    批量回填所有股票的财务指标

    Parameters
    ----------
    start_date : str  起始日期 (YYYYMMDD)
    limit : int  限制股票数（调试用）
    """
    from data.storage import list_cached_stocks

    pro = _init_tushare()
    stocks = list_cached_stocks()
    if limit > 0:
        stocks = stocks[:limit]

    print(f"开始拉取 {len(stocks)} 只股票财务指标 (起始 {start_date})")
    print("Tushare fina_indicator 限流 200 次/分钟，预计 ~30 分钟")

    success = 0
    fail = 0
    total_rows = 0
    t0 = time.time()

    for i, code in enumerate(stocks, 1):
        # 转 ts_code 格式
        prefix = "SH" if code.startswith(("6", "5")) else "SZ"
        # 北交所 920/4xx/8xx
        if code.startswith(("4", "8", "92")):
            prefix = "BJ"
        ts_code = f"{code}.{prefix}"

        df = fetch_one_stock(pro, ts_code, start_date)
        if df.empty:
            fail += 1
        else:
            df = df[df["ann_date"].notna()]  # 去掉 ann_date 为空的脏数据
            df["code"] = code
            rows = df.to_dict("records")
            n = save_batch(rows)
            total_rows += n
            success += 1

        if i % 100 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed
            eta = (len(stocks) - i) / rate / 60
            print(f"  [{i}/{len(stocks)}] ok={success} fail={fail} 行数={total_rows} eta~{eta:.0f}min")

        # 限流: 200 次/分钟 → 0.3s/次
        time.sleep(0.3)

    print(f"\n完成: ok={success} fail={fail} 行数={total_rows}")


def get_latest_pit(code: str, as_of_date: str) -> dict:
    """
    获取股票在 as_of_date 时的最新可用财务数据 (PIT)

    Parameters
    ----------
    code : 股票代码
    as_of_date : 截面日期 (YYYY-MM-DD 或 YYYYMMDD)

    Returns
    -------
    dict: {roe_yearly, or_yoy, dt_eps_yoy, debt_to_assets, ...}
    无可用数据返回 {}
    """
    # 标准化日期格式
    as_of_date = as_of_date.replace("-", "")

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            """SELECT roe_yearly, or_yoy, dt_eps_yoy, debt_to_assets,
                      netprofit_yoy, op_yoy, netprofit_margin
            FROM financial_indicator
            WHERE code = ? AND ann_date <= ?
            ORDER BY ann_date DESC, end_date DESC
            LIMIT 1""",
            (code, as_of_date),
        )
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "roe_yearly": row[0],
            "or_yoy": row[1],
            "dt_eps_yoy": row[2],
            "debt_to_assets": row[3],
            "netprofit_yoy": row[4],
            "op_yoy": row[5],
            "netprofit_margin": row[6],
        }
    finally:
        conn.close()


def load_all_pit_to_dict() -> dict:
    """
    一次性加载所有 PIT 数据到内存 → {code: [(ann_date, factor_dict), ...]}

    用于训练时高效查询，每只股票按 ann_date 排序支持二分查找。
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            """SELECT code, ann_date, roe_yearly, or_yoy, dt_eps_yoy,
                      debt_to_assets
               FROM financial_indicator
               ORDER BY code, ann_date"""
        )
        result = {}  # code → [(ann_date, dict), ...]
        for row in cur.fetchall():
            code = row[0]
            entry = (row[1], {
                "roe_yearly": row[2], "or_yoy": row[3],
                "dt_eps_yoy": row[4], "debt_to_assets": row[5],
            })
            result.setdefault(code, []).append(entry)
        return result
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def get_coverage() -> dict:
    """统计覆盖率"""
    conn = sqlite3.connect(DB_PATH)
    try:
        result = conn.execute("""
            SELECT
                COUNT(*) as total_rows,
                COUNT(DISTINCT code) as unique_codes,
                MIN(ann_date) as min_ann,
                MAX(ann_date) as max_ann,
                COUNT(roe_yearly) as roe_non_null,
                COUNT(or_yoy) as or_yoy_non_null
            FROM financial_indicator
        """).fetchone()
        return dict(zip(
            ["total_rows", "unique_codes", "min_ann", "max_ann",
             "roe_non_null", "or_yoy_non_null"],
            result,
        ))
    finally:
        conn.close()


def run():
    """命令行入口"""
    batch_fetch_all()


if __name__ == "__main__":
    run()
