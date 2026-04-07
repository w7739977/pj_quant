"""数据质量验证脚本"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from data.storage import list_cached_stocks, load_stock_daily
import baostock as bs

print("=" * 60)
print("数据质量验证报告")
print("=" * 60)

cached = list_cached_stocks()
print(f"\n1. 本地缓存: {len(cached)} 只股票")

# 2. 抽样100只检查
sample = cached[:100]
lengths = []
null_counts = []
for code in sample:
    df = load_stock_daily(code)
    if df.empty:
        continue
    lengths.append(len(df))
    null_counts.append(df.isnull().sum().sum())

print(f"\n2. 抽样100只数据质量:")
print(f"  数据条数: min={min(lengths)}, max={max(lengths)}, avg={np.mean(lengths):.0f}")
print(f"  空值统计: {sum(null_counts)} 个空值")

# 3. 对比BaoStock在线数据
print(f"\n3. 对比BaoStock在线数据 (验证准确性)...")
lg = bs.login()

for code in ["000001", "600519", "300750"]:
    prefix = "sh" if code.startswith("6") else "sz"
    bs_code = f"{prefix}.{code}"
    rs = bs.query_history_k_data_plus(
        bs_code,
        "date,open,high,low,close,volume,turn,pctChg",
        start_date="2026-03-20", end_date="2026-04-03",
        frequency="d", adjustflag="2",
    )
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if rows:
        bs_df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume", "turn", "pctChg"])
        for c in ["open", "high", "low", "close", "volume", "turn", "pctChg"]:
            bs_df[c] = pd.to_numeric(bs_df[c], errors="coerce")
        bs_df["date"] = pd.to_datetime(bs_df["date"])
        local_df = load_stock_daily(code)
        local_df = local_df[local_df["date"] >= "2026-03-20"]
        if not local_df.empty and not bs_df.empty:
            merged = local_df.tail(5)[["date", "close"]].merge(
                bs_df.tail(5)[["date", "close"]].rename(columns={"close": "bs_close"})
            )
            merged["diff"] = abs(merged["close"] - merged["bs_close"])
            max_diff = merged["diff"].max()
            match_pct = (merged["diff"] < 0.01).sum() / len(merged)
            local_prices = [round(x, 2) for x in merged["close"].tolist()]
            bs_prices = [round(x, 2) for x in merged["bs_close"].tolist()]
            print(f"  {code}: 匹配率={match_pct:.0%}, 最大差异={max_diff:.4f}")
            print(f"    本地收盘: {local_prices}")
            print(f"    在线收盘: {bs_prices}")
        else:
            print(f"  {code}: 无法对比 (本地={len(local_df)}, 在线={len(bs_df)})")
    else:
        print(f"  {code}: BaoStock无数据")

bs.logout()

# 4. 统计
print(f"\n4. 总体统计:")
sample_dates = [load_stock_daily(c)["date"].max() for c in cached[:10]]
date_strs = [d.strftime("%Y-%m-%d") for d in sample_dates]
print(f"  最新日期样本: {date_strs}")
sample_mins = [load_stock_daily(c)["date"].min() for c in cached[:10]]
min_strs = [d.strftime("%Y-%m-%d") for d in sample_mins]
print(f"  最早日期样本: {min_strs}")
