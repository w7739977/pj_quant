"""
批量行情数据获取 — BaoStock 持久连接

功能:
  1. 获取全部 A 股股票列表（排除 ST/退市/北交所/科创板）
  2. 持久 BaoStock 连接，逐只拉取日线
  3. 增量入库 SQLite（已有数据跳过或追加新日期）
  4. 支持断点续传

用法:
  python -m data.bulk_fetcher              # 全量拉取
  python -m data.bulk_fetcher --refresh    # 强制刷新已有数据
  python -m data.bulk_fetcher --limit 100  # 只拉前100只（调试）
"""

import sys
import time
import logging
import baostock as bs
import pandas as pd
from datetime import datetime

from data.storage import save_stock_daily, load_stock_daily, list_cached_stocks

logger = logging.getLogger(__name__)


def get_all_stock_codes() -> list:
    """
    获取全部 A 股代码（BaoStock）

    Returns
    -------
    list of str: 纯数字代码列表，如 ['000001', '000002', ...]
    """
    rs = bs.query_stock_basic()
    stock_list = []
    while rs.error_code == "0" and rs.next():
        row = rs.get_row_data()
        # row: [code, name, ipoDate, outDate, type, tradeStatus]
        code = row[0] if len(row) > 0 else ""
        name = row[1] if len(row) > 1 else ""
        stype = row[4] if len(row) > 4 else ""
        trade_status = row[5] if len(row) > 5 else ""

        # type=1(股票) + tradeStatus=1(上市), 排除 ST/退市/北交所/科创板
        if stype == "1" and trade_status == "1" and "ST" not in name and "退" not in name:
            pure_code = code.split(".")[-1] if "." in code else code
            if pure_code.startswith(("8", "688", "9")):
                continue
            stock_list.append(pure_code)

    return stock_list


def fetch_stock_daily(symbol: str, start_date: str = "2020-01-01",
                      end_date: str = None) -> pd.DataFrame:
    """
    BaoStock 获取单只股票日线（需已 login）

    Parameters
    ----------
    symbol : str  纯数字代码，如 '000001'
    start_date : str
    end_date : str

    Returns
    -------
    pd.DataFrame: columns [date, open, high, low, close, volume, turnover, pct_chg]
    """
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    prefix = "sh" if symbol.startswith("6") else "sz"
    bs_code = f"{prefix}.{symbol}"

    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,turn,pctChg",
        start_date=start_date, end_date=end_date,
        frequency="d", adjustflag="2",
    )
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                      "volume", "turnover", "pct_chg"])
    for c in ["open", "high", "low", "close", "volume", "turnover", "pct_chg"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def bulk_fetch(limit: int = 0, refresh: bool = False):
    """
    批量拉取全市场日线数据（持久连接，断点续传）

    Parameters
    ----------
    limit : int  最多拉取几只，0=全部
    refresh : bool  True=强制刷新已有数据
    """
    lg = bs.login()
    if lg.error_code != "0":
        print(f"BaoStock 登录失败: {lg.error_msg}")
        return

    try:
        # 获取股票列表
        all_codes = get_all_stock_codes()
        total = len(all_codes)
        if limit > 0:
            all_codes = all_codes[:limit]
            total = min(total, limit)

        # 断点续传：跳过已缓存股票（除非 refresh）
        cached = set(list_cached_stocks()) if not refresh else set()
        to_fetch = [c for c in all_codes if c not in cached]

        skipped = len(all_codes) - len(to_fetch)
        print(f"\n{'='*60}")
        print(f"批量数据获取 - BaoStock 持久连接")
        print(f"{'='*60}")
        print(f"  全市场股票: {total} 只")
        print(f"  已缓存(跳过): {skipped} 只")
        print(f"  待获取: {len(to_fetch)} 只")
        print(f"{'='*60}\n")

        success = 0
        fail = 0
        start_time = time.time()

        for i, code in enumerate(to_fetch):
            try:
                df = fetch_stock_daily(code)
                if not df.empty:
                    save_stock_daily(df, code)
                    success += 1
                else:
                    fail += 1
                    logger.debug(f"{code}: 无数据")
            except Exception as e:
                fail += 1
                logger.warning(f"{code}: {e}")
                # 遇到连接错误，重新登录
                if "connect" in str(e).lower() or "timeout" in str(e).lower():
                    time.sleep(2)
                    bs.logout()
                    time.sleep(1)
                    bs.login()

            # 进度报告
            if (i + 1) % 100 == 0 or (i + 1) == len(to_fetch):
                elapsed = time.time() - start_time
                rate = (i + 1) / elapsed if elapsed > 0 else 0
                eta = (len(to_fetch) - i - 1) / rate if rate > 0 else 0
                print(f"  [{i+1}/{len(to_fetch)}] "
                      f"成功={success} 失败={fail} "
                      f"速度={rate:.1f}只/s "
                      f"剩余≈{eta/60:.0f}min")

            # 控制频率，避免被限流
            if (i + 1) % 500 == 0:
                time.sleep(1)

        elapsed = time.time() - start_time
        print(f"\n{'='*60}")
        print(f"批量获取完成")
        print(f"  总耗时: {elapsed/60:.1f} 分钟")
        print(f"  成功: {success} | 失败: {fail}")
        print(f"  本地已缓存: {len(list_cached_stocks())} 只股票")
        print(f"{'='*60}")

    finally:
        bs.logout()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    _limit = 0
    _refresh = False

    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        _limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 100

    if "--refresh" in sys.argv:
        _refresh = True

    bulk_fetch(limit=_limit, refresh=_refresh)
