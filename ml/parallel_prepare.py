"""
并行数据准备 — 为模型训练生成数据

将 4417 只股票分成 N 个 chunk 并行处理:
  1. 每个进程处理一部分股票
  2. 生成训练样本，保存为 Parquet
  3. 最后合并成完整训练集

用法:
  # 主进程: 拆分股票列表
  python3 -c "from ml.parallel_prepare import split_chunks; split_chunks(4)"

  # Agent 0-3: 处理各自 chunk
  python3 -c "from ml.parallel_prepare import prepare_chunk; prepare_chunk(0)"
"""

import os
import sys
sys.path.insert(0, '.')
import sqlite3
import pandas as pd
import numpy as np
from data.storage import load_stock_daily, list_cached_stocks
from factors.calculator import (
    calc_momentum, calc_volatility, calc_turnover_factor,
    calc_volume_price, calc_technical,
)

CHUNK_DIR = "ml/training_chunks"
os.makedirs(CHUNK_DIR, exist_ok=True)

FORWARD_DAYS = 20


def split_chunks(n_chunks: int = 4):
    """将股票列表分成 N 个 chunk"""
    cached = list_cached_stocks()
    total = len(cached)
    chunk_size = total // n_chunks + 1

    for i in range(n_chunks):
        start = i * chunk_size
        end = min((i + 1) * chunk_size, total)
        chunk_codes = cached[start:end]

        chunk_file = os.path.join(CHUNK_DIR, f"chunk_{i}_codes.txt")
        with open(chunk_file, 'w') as f:
            f.write('\n'.join(chunk_codes))

        print(f"Chunk {i}: {len(chunk_codes)} 只股票 → {chunk_file}")


def prepare_chunk(chunk_id: int):
    """处理一个 chunk 的股票，生成训练样本"""
    chunk_file = os.path.join(CHUNK_DIR, f"chunk_{chunk_id}_codes.txt")
    if not os.path.exists(chunk_file):
        print(f"Chunk {chunk_id}: 文件不存在")
        return

    with open(chunk_file) as f:
        codes = [line.strip() for line in f if line.strip()]

    total = len(codes)
    records = []

    for i, sym in enumerate(codes):
        try:
            df = load_stock_daily(sym)
            if df.empty or len(df) < 120:
                continue

            # 滚动截面：每 20 天取一个样本
            for end_idx in range(60, len(df) - FORWARD_DAYS, 20):
                window = df.iloc[:end_idx + 1]
                fwd = df.iloc[end_idx:end_idx + FORWARD_DAYS + 1]
                if len(fwd) < FORWARD_DAYS + 1:
                    continue

                forward_return = float(fwd.iloc[-1]["close"]) / float(fwd.iloc[0]["close"]) - 1.0

                factors = {"code": sym, "label": forward_return}
                factors.update(calc_momentum(window))
                factors.update(calc_volatility(window))
                factors.update(calc_turnover_factor(window))
                factors.update(calc_volume_price(window))
                factors.update(calc_technical(window))

                # 基本面因子
                last_row = window.iloc[-1]
                factors["pe_ttm"] = last_row.get("pe_ttm", np.nan)
                factors["pb"] = last_row.get("pb", np.nan)
                factors["turnover_rate"] = last_row.get("turnover_rate", np.nan)
                factors["volume_ratio"] = last_row.get("volume_ratio", np.nan)
                factors["sentiment_score"] = np.nan

                records.append(factors)
        except Exception as e:
            continue

        if (i + 1) % 200 == 0 or (i + 1) == total:
            print(f"  Chunk {chunk_id}: [{i+1}/{total}] 已生成 {len(records)} 条样本")

    # 保存为 Parquet
    if records:
        df = pd.DataFrame(records)
        parquet_path = os.path.join(CHUNK_DIR, f"chunk_{chunk_id}.parquet")
        df.to_parquet(parquet_path, index=False)
        print(f"Chunk {chunk_id} 完成: {len(df)} 条样本 → {parquet_path}")
        return len(df)
    else:
        print(f"Chunk {chunk_id}: 无样本生成")
        return 0


def merge_chunks():
    """合并所有 chunk 的 Parquet 文件"""
    import glob

    parquet_files = glob.glob(os.path.join(CHUNK_DIR, "chunk_*.parquet"))
    if not parquet_files:
        print("没有 chunk 文件")
        return pd.DataFrame()

    dfs = []
    for fpath in sorted(parquet_files):
        dfs.append(pd.read_parquet(fpath))

    merged = pd.concat(dfs, ignore_index=True)
    print(f"合并完成: {len(merged)} 条训练样本")
    return merged


if __name__ == "__main__":
    import sys
    chunk_id = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    prepare_chunk(chunk_id)
