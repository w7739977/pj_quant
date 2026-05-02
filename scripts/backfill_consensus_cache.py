"""回填 daily_scored_cache，用于共识选股冷启动

避免等 5 个工作日累积——用历史数据 + 当前生产模型一次性补足。

用法:
  python3 scripts/backfill_consensus_cache.py              # 默认回填过去 10 个交易日
  python3 scripts/backfill_consensus_cache.py --days 20    # 回填 20 个交易日
  python3 scripts/backfill_consensus_cache.py --top-n 10   # 每天缓存 top 10

注意:
  - 用今日生产模型对历史日期重算 final_score。这意味着 cache 是"假设今日模型在那天"的预测，
    不代表当时实际会选出的股票。
  - 对共识选股而言这没问题——它只关心"过去 5 天哪些股票稳定上榜"，模型版本一致比时间正确更重要。
  - 之后每天 cron 跑 monitor-only / consensus 时会自动维护 cache。
"""
import os
import sys
import sqlite3
import argparse
import warnings
import logging
from datetime import datetime

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

import pandas as pd
import numpy as np
from xgboost import XGBRegressor

import ml.ranker as r
from ml.ranker import FEATURE_COLS, PRODUCTION_MODEL
from factors.calculator import (
    calc_momentum, calc_volatility, calc_turnover_factor,
    calc_volume_price, calc_technical, winsorize_cross_section,
)
from factors.data_loader import get_small_cap_stocks
from data.storage import load_stock_daily
from data.financial_indicator import load_all_pit_to_dict
from strategy.small_cap import SmallCapStrategy
from portfolio.consensus import cache_scored, cache_stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=10,
                        help="回填多少个交易日（按 stock_000001 真实交易日，不含今天）")
    parser.add_argument("--top-n", type=int, default=10,
                        help="每天缓存 top N 只")
    parser.add_argument("--end", type=str, default=None,
                        help="回填到此日期为止（YYYY-MM-DD），默认到昨天")
    args = parser.parse_args()

    end_date = args.end or datetime.now().strftime("%Y-%m-%d")
    print(f"=== 回填 daily_scored_cache ===")
    print(f"  目标: 过去 {args.days} 个交易日 (< {end_date})")
    print(f"  top_n: {args.top_n}")

    print(f"\n[1/4] 加载财务 PIT 缓存...")
    r._FIN_CACHE = load_all_pit_to_dict()
    print(f"  {len(r._FIN_CACHE)} 只股票")

    print(f"\n[2/4] 加载生产模型...")
    if not os.path.exists(PRODUCTION_MODEL):
        print(f"  ❌ 模型不存在: {PRODUCTION_MODEL}")
        print(f"  请先 python3 main.py evolve")
        return 1
    model = XGBRegressor()
    model.load_model(PRODUCTION_MODEL)
    print(f"  ✓ {PRODUCTION_MODEL}")

    print(f"\n[3/4] 取目标交易日...")
    conn = sqlite3.connect("data/quant.db")
    dates = [str(row[0])[:10] for row in conn.execute(
        "SELECT DISTINCT date FROM stock_000001 WHERE date < ? ORDER BY date DESC LIMIT ?",
        (end_date, args.days),
    ).fetchall()]
    dates.reverse()  # 时间顺序
    print(f"  {len(dates)} 个交易日: {dates[0]} ~ {dates[-1]}")

    # 股票池：用当前市值（轻微 forward leak，但 D 方案 cache 用足够）
    pool = get_small_cap_stocks(5e8, 5e9)
    symbols = pool["code"].tolist()
    print(f"  股票池: {len(symbols)} 只")

    print(f"\n[4/4] 预加载日线数据...")
    stock_data = {}
    for sym in symbols:
        df = load_stock_daily(sym)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["date_str"] = df["date"].astype(str).str[:10]
        stock_data[sym] = df
    print(f"  {len(stock_data)} 只可用")

    print(f"\n=== 开始按日回填 ===")
    for D in dates:
        rows = []
        for sym, df in stock_data.items():
            win = df[df["date_str"] <= D].tail(120)
            if len(win) < 20:
                continue
            f = {"code": sym}
            f.update(calc_momentum(win))
            f.update(calc_volatility(win))
            f.update(calc_turnover_factor(win))
            f.update(calc_volume_price(win))
            f.update(calc_technical(win))
            last = win.iloc[-1]
            for col in ["pe_ttm", "pb", "turnover_rate", "volume_ratio"]:
                f[col] = last.get(col, np.nan)
            fin = r._lookup_financial_pit(sym, D.replace("-", ""))
            for col in ["roe_yearly", "or_yoy", "dt_eps_yoy", "debt_to_assets"]:
                f[col] = fin.get(col, np.nan)
            rows.append(f)

        fdf = pd.DataFrame(rows)
        if fdf.empty:
            print(f"  [{D}] 无样本，跳过")
            continue

        fdf = winsorize_cross_section(
            fdf, ["roe_yearly", "or_yoy", "dt_eps_yoy", "debt_to_assets", "pe_ttm", "pb"],
            lower=0.01, upper=0.99,
        )
        X = fdf[FEATURE_COLS].copy().fillna(fdf[FEATURE_COLS].median())
        fdf["pred"] = model.predict(X)

        sc = SmallCapStrategy()
        scored = sc._score_stocks(fdf).reset_index(drop=True)
        pred_s = scored["pred"]
        ml_norm = (pred_s - pred_s.mean()) / (pred_s.std() + 1e-8)
        factor_norm = (scored["score"] - scored["score"].mean()) / (scored["score"].std() + 1e-8)
        scored["final_score"] = ml_norm * 0.5 + factor_norm * 0.5

        n = cache_scored(D, scored[["code", "final_score"]], top_n=args.top_n)
        print(f"  [{D}] cache 入库 top {n}")

    print(f"\n=== 完成 ===")
    stats = cache_stats()
    print(f"  cache 状态: {stats}")
    print(f"\n下次跑 `python3 main.py live --consensus` 即可使用共识选股（不再回退）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
