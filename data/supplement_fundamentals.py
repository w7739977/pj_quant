"""
补全估值数据 — 为每只股票补充 peTTM, pbMRQ, psTTM, pcfNcfTTM

从 BaoStock query_history_k_data_plus 获取，写入本地 SQLite。

用法:
  python3 -c "from data.supplement_fundamentals import supplement_fundamentals; supplement_fundamentals(limit=5)"
  python3 -c "from data.supplement_fundamentals import supplement_fundamentals; supplement_fundamentals()"  # 全量
"""

import time
import logging
import sqlite3
import pandas as pd
import baostock as bs
from data.storage import get_connection, list_cached_stocks

logger = logging.getLogger(__name__)

FUND_COLS = {
    "peTTM": "pe_ttm",
    "pbMRQ": "pb",
    "psTTM": "ps_ttm",
    "pcfNcfTTM": "pcf_ncf_ttm",
}
BS_FIELDS = "date," + ",".join(FUND_COLS.keys())


def _code_to_bs(code: str) -> str:
    return f"sh.{code}" if code.startswith("6") else f"sz.{code}"


def _ensure_columns(conn: sqlite3.Connection, table: str):
    """确保表有估值列"""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    existing = {row[1] for row in cursor.fetchall()}
    for col in FUND_COLS.values():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} REAL")
    conn.commit()


def supplement_fundamentals(limit: int = 0):
    """批量补全估值数据"""
    lg = bs.login()
    if lg.error_code != "0":
        print(f"BaoStock 登录失败: {lg.error_msg}")
        return

    cached = list_cached_stocks()
    if limit > 0:
        cached = cached[:limit]

    total = len(cached)
    print(f"\n{'='*60}")
    print(f"补全估值数据 - {total} 只股票")
    print(f"{'='*60}\n")

    success = 0
    fail = 0
    start_time = time.time()

    for i, code in enumerate(cached):
        try:
            bs_code = _code_to_bs(code)
            rs = bs.query_history_k_data_plus(
                bs_code, BS_FIELDS,
                start_date="2020-01-01", end_date="2099-12-31",
                frequency="d", adjustflag="2",
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())

            if not rows:
                fail += 1
                continue

            # BaoStock 返回的 DataFrame
            col_names = ["date"] + list(FUND_COLS.values())
            fund_df = pd.DataFrame(rows, columns=col_names)
            for c in FUND_COLS.values():
                fund_df[c] = pd.to_numeric(fund_df[c], errors="coerce")
            fund_df["date_str"] = fund_df["date"].str[:10]

            # 写入本地
            conn = get_connection()
            table = f"stock_{code}"
            _ensure_columns(conn, table)

            # 读本地 date → 匹配
            local_dates = pd.read_sql(f"SELECT rowid, date FROM {table}", conn)
            local_dates["date_str"] = local_dates["date"].astype(str).str[:10]

            matched = local_dates.merge(fund_df, on="date_str", how="inner")

            if not matched.empty:
                # 批量 UPDATE
                updates = []
                for _, row in matched.iterrows():
                    vals = tuple(
                        None if pd.isna(row[col]) else float(row[col])
                        for col in FUND_COLS.values()
                    ) + (int(row["rowid"]),)
                    updates.append(vals)

                placeholders = ", ".join(FUND_COLS.values())
                set_clause = ", ".join(f"{col}=?" for col in FUND_COLS.values())
                conn.executemany(
                    f"UPDATE {table} SET {set_clause} WHERE rowid=?",
                    updates,
                )
                conn.commit()
                success += 1
            else:
                fail += 1

            conn.close()

        except Exception as e:
            fail += 1
            logger.debug(f"{code}: {e}")

        if (i + 1) % 100 == 0 or (i + 1) == total:
            elapsed = time.time() - start_time
            rate = (i + 1) / elapsed if elapsed > 0 else 999
            eta = (total - i - 1) / rate if rate > 0 else 0
            print(f"  [{i+1}/{total}] 成功={success} 失败={fail} "
                  f"速度={rate:.1f}只/s 剩余~{eta/60:.0f}min")

        if (i + 1) % 500 == 0:
            time.sleep(1)

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"补全完成: 成功={success} 失败={fail} 耗时={elapsed/60:.1f}分钟")
    print(f"{'='*60}")

    bs.logout()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    import sys
    _limit = 0
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        _limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 100
    supplement_fundamentals(limit=_limit)
