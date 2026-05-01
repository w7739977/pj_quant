"""
Tushare 行业分类数据 — 一次性拉取入库
约 5500 只股票 → industry 字段（如 "医药生物"、"银行"）
"""
import os, sqlite3, pandas as pd
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "data", "quant.db")


def _init_tushare():
    from data.tushare_fundamentals import _init_tushare
    return _init_tushare()


def _init_industry_table(conn):
    """创建 industry_map 汇总表（idempotent）"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS industry_map (
            code TEXT PRIMARY KEY,
            name TEXT,
            industry TEXT,
            area TEXT,
            list_date TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()


def fetch_and_save_industry():
    """
    一次性获取全市场股票 → 行业映射，写入 SQLite industry_map 表

    Tushare stock_basic 返回字段:
      ts_code, symbol, name, area, industry, list_date, market, ...
    """
    pro = _init_tushare()
    conn = sqlite3.connect(DB_PATH)
    try:
        _init_industry_table(conn)

        # 一次拉取所有 listed 股票（不区分市场）
        df = pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,area,industry,list_date,market",
        )

        if df is None or df.empty:
            print("Tushare stock_basic 返回为空")
            return 0

        # ts_code 转纯 6 位 code (000001.SZ → 000001)
        df["code"] = df["ts_code"].str.split(".").str[0]
        # 缺失的行业填 "未知"
        df["industry"] = df["industry"].fillna("未知")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            (row["code"], row["name"], row["industry"],
             row.get("area", ""), row.get("list_date", ""), now)
            for _, row in df.iterrows()
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO industry_map "
            "(code, name, industry, area, list_date, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        print(f"行业映射已入库: {len(rows)} 只股票")

        # 行业分布统计
        ind_count = df["industry"].value_counts().head(10)
        print(f"\nTop 10 行业:")
        for ind, n in ind_count.items():
            print(f"  {ind}: {n} 只")

        return len(rows)
    finally:
        conn.close()


def load_industry_map() -> dict:
    """读取 SQLite industry_map → {code: industry}"""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("SELECT code, industry FROM industry_map")
        return {code: ind for code, ind in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def get_industry_for_codes(codes: list) -> dict:
    """批量获取代码 → 行业映射（仅查询，不联网）"""
    if not codes:
        return {}
    conn = sqlite3.connect(DB_PATH)
    try:
        placeholders = ",".join("?" * len(codes))
        cur = conn.execute(
            f"SELECT code, industry FROM industry_map WHERE code IN ({placeholders})",
            codes,
        )
        return {code: ind for code, ind in cur.fetchall()}
    finally:
        conn.close()


def run():
    """命令行入口"""
    fetch_and_save_industry()


if __name__ == "__main__":
    run()
