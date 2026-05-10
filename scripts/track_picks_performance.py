"""Picks 表现追踪：按周一 D 共识 picks 视角，per-pick 详细评估

不开实盘的情况下验证模型推荐股票的"如果跑实盘会怎样"。
对每个周一 (本周第一个交易日) 重建 D 共识 picks，算每只 5d 实际收益，
累积出 per-pick 胜率 / profit factor / expectancy / 最大赢家/输家。

输入: 同 backtest_year.py — 财务 PIT cache + 生产模型 + 股票池
输出:
  - logs/picks_history_backtest.csv: per-pick 明细 (date, code, rank, freq, ret_5d, bench_5d, alpha)
  - docs/picks_performance.md: 报告

用法:
  python3 scripts/track_picks_performance.py
  python3 scripts/track_picks_performance.py --start 2026-01-01 --end 2026-04-23
"""
import argparse
import logging
import os
import sqlite3
import sys
import warnings
from collections import Counter
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore")
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.chdir(REPO)
sys.path.insert(0, str(REPO / ".claude/skills/portfolio-analytics/scripts"))

logging.basicConfig(level=logging.WARNING)

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

import ml.ranker as r
from analyze_portfolio import trade_statistics  # noqa: E402
from data.financial_indicator import load_all_pit_to_dict
from data.storage import load_stock_daily
from factors.calculator import (
    calc_momentum, calc_volatility, calc_turnover_factor,
    calc_volume_price, calc_technical, winsorize_cross_section,
)
from factors.data_loader import get_small_cap_stocks
from ml.ranker import FEATURE_COLS, PRODUCTION_MODEL
from portfolio.consensus import is_window_fresh
from strategy.small_cap import SmallCapStrategy

TOP_N = 10
HOLD = 5
LATEST_FOR_POOL = "2026-04-25"


def build_daily_scored(model, stock_data, dates, fin_pool):
    """对每一日 D，算全市场 final_score (复用 backtest_year 逻辑)"""
    daily = {}
    for D in dates:
        D_ts = pd.Timestamp(D)
        rows = []
        for sym, df in stock_data.items():
            win = df[df["date_str"] <= D].tail(120)
            if len(win) < 20:
                continue
            if not is_window_fresh(win.iloc[-1]["date_str"], D_ts):
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
        if not rows:
            continue
        fdf = pd.DataFrame(rows)
        fdf = winsorize_cross_section(
            fdf, ["roe_yearly", "or_yoy", "dt_eps_yoy", "debt_to_assets", "pe_ttm", "pb"],
            lower=0.01, upper=0.99,
        )
        X = fdf[FEATURE_COLS].copy().fillna(fdf[FEATURE_COLS].median())
        fdf["pred"] = model.predict(X)
        sc = SmallCapStrategy()
        scored = sc._score_stocks(fdf).reset_index(drop=True)
        ml_norm = (scored["pred"] - scored["pred"].mean()) / (scored["pred"].std() + 1e-8)
        factor_norm = (scored["score"] - scored["score"].mean()) / (scored["score"].std() + 1e-8)
        scored["final_score"] = ml_norm * 0.5 + factor_norm * 0.5
        daily[D] = scored[["code", "final_score"]]
    return daily


def fwd_return(stock_data, code, D, hold=HOLD):
    df = stock_data.get(code)
    if df is None:
        return None
    before = df[df["date_str"] <= D].tail(1)
    after = df[df["date_str"] > D].head(hold)
    if len(before) == 1 and len(after) >= hold:
        p0 = before.iloc[0]["close"]
        p1 = after.iloc[hold - 1]["close"]
        if p0 > 0:
            return p1 / p0 - 1
    return None


def consensus_picks_for(D, daily_scored, buffer_dates, top_n=TOP_N, window=5):
    """重建周一 D 时刻的 D 频次共识 picks (与生产 portfolio.consensus 一致)"""
    if D not in buffer_dates:
        return []
    idx = buffer_dates.index(D)
    if idx < window:
        return []
    prev = buffer_dates[idx - window: idx]
    counter: Counter = Counter()
    score_sum: dict[str, list[float]] = {}
    for pd_ in prev:
        if pd_ not in daily_scored:
            continue
        top = daily_scored[pd_].sort_values("final_score", ascending=False).head(top_n)
        for _, row in top.iterrows():
            counter[row["code"]] += 1
            score_sum.setdefault(row["code"], []).append(row["final_score"])
    ranked = sorted(
        counter.items(),
        key=lambda x: (-x[1], -float(np.mean(score_sum[x[0]]))),
    )
    return [
        {"code": c, "freq": counter[c],
         "avg_score": float(np.mean(score_sum[c]))}
        for c, _ in ranked[:top_n]
    ]


def benchmark_5d(stock_data, D):
    """同期池等权 5d 收益 (与 backtest_year 一致的 baseline)"""
    rs = [fwd_return(stock_data, c, D) for c in stock_data]
    rs = [x for x in rs if x is not None]
    return np.mean(rs) if rs else np.nan


def main():
    parser = argparse.ArgumentParser()
    today = datetime.now().strftime("%Y-%m-%d")
    parser.add_argument("--start", default=f"{datetime.now().year}-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--out-csv", default="logs/picks_history_backtest.csv")
    parser.add_argument("--out-md", default="docs/picks_performance.md")
    args = parser.parse_args()

    print(f"=== picks 表现追踪 ===")
    print(f"  start={args.start}, end={args.end or '自动'}")

    print("\n[1/5] 加载财务 + 模型 + 池...")
    r._FIN_CACHE = load_all_pit_to_dict()
    if not os.path.exists(PRODUCTION_MODEL):
        print(f"❌ 模型缺失: {PRODUCTION_MODEL}")
        return 1
    model = XGBRegressor()
    model.load_model(PRODUCTION_MODEL)

    pool = get_small_cap_stocks(5e8, 5e9)
    print(f"  池: {len(pool)} 只")

    print("\n[2/5] 取交易日 + 预加载日线...")
    conn = sqlite3.connect("data/quant.db")
    all_dates = [str(row[0])[:10] for row in conn.execute(
        "SELECT DISTINCT date FROM stock_000001 WHERE date >= ? ORDER BY date",
        (f"{int(args.start[:4]) - 1}-12-15",),
    ).fetchall()]
    if args.end:
        end_date = args.end
    else:
        future = [d for d in all_dates if d > today]
        end_date = future[4] if len(future) >= 5 else sorted(d for d in all_dates if d <= today)[-6]
    decision_dates = [d for d in all_dates if args.start <= d <= end_date]
    buffer_dates = [d for d in all_dates if d <= end_date]

    stock_data = {}
    for sym in pool["code"].tolist():
        df = load_stock_daily(sym)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["date_str"] = df["date"].astype(str).str[:10]
        if df["date_str"].max() < LATEST_FOR_POOL:
            continue
        stock_data[sym] = df
    print(f"  stock_data: {len(stock_data)} 只 / 决策日 {len(decision_dates)}")

    print(f"\n[3/5] 算每日 final_score ({len(buffer_dates)} 日)...")
    daily_scored = build_daily_scored(model, stock_data, buffer_dates, pool)

    mondays = [d for d in decision_dates
               if datetime.strptime(d, "%Y-%m-%d").weekday() == 0]
    print(f"  周一: {len(mondays)} 个")

    print("\n[4/5] 对每周一构建 picks + 算 5d 收益...")
    rows = []
    for D in mondays:
        picks = consensus_picks_for(D, daily_scored, buffer_dates)
        if not picks:
            continue
        bench = benchmark_5d(stock_data, D)
        for rank, p in enumerate(picks, 1):
            ret = fwd_return(stock_data, p["code"], D)
            if ret is None:
                continue
            rows.append({
                "monday": D, "rank": rank, "code": p["code"],
                "freq": p["freq"], "avg_score": round(p["avg_score"], 4),
                "ret_5d": ret, "bench_5d": bench,
                "alpha": ret - bench if not pd.isna(bench) else np.nan,
            })
    df = pd.DataFrame(rows)
    if df.empty:
        print("❌ 无有效 picks")
        return 1
    os.makedirs(os.path.dirname(args.out_csv), exist_ok=True)
    df.to_csv(args.out_csv, index=False)

    print(f"\n[5/5] 生成报告...")
    write_report(df, args.out_md)
    print(f"\n  CSV: {args.out_csv}  ({len(df)} picks)")
    print(f"  MD : {args.out_md}")
    return 0


def write_report(df: pd.DataFrame, out_md: str) -> None:
    n_weeks = df["monday"].nunique()
    n_picks = len(df)
    win_rate = (df["ret_5d"] > 0).mean()
    avg_ret = df["ret_5d"].mean()
    avg_alpha = df["alpha"].mean()

    pnl = df["ret_5d"]
    stats = trade_statistics(pnl)

    # 周度组合等权
    weekly = df.groupby("monday").agg(
        n_picks=("code", "count"),
        avg_ret=("ret_5d", "mean"),
        bench=("bench_5d", "first"),
        win_n=("ret_5d", lambda x: (x > 0).sum()),
    ).reset_index()
    weekly["alpha"] = weekly["avg_ret"] - weekly["bench"]
    cum_ret = (1 + weekly["avg_ret"]).cumprod() - 1
    cum_alpha = (1 + weekly["alpha"]).cumprod() - 1

    # 排名维度的胜率（rank 1 vs rank 10）
    by_rank = df.groupby("rank").agg(
        n=("code", "count"),
        avg_ret=("ret_5d", "mean"),
        win_rate=("ret_5d", lambda x: (x > 0).mean()),
        avg_alpha=("alpha", "mean"),
    ).round(4)

    top_winners = df.nlargest(10, "ret_5d")[["monday", "rank", "code", "freq", "ret_5d", "alpha"]]
    top_losers = df.nsmallest(10, "ret_5d")[["monday", "rank", "code", "freq", "ret_5d", "alpha"]]

    md = f"""# Picks 表现追踪 (历史回测视角)

**生成日期**: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}
**回测区间**: {df['monday'].min()} ~ {df['monday'].max()}
**模型**: `xgb_ranker.json` (PRODUCTION_MODEL)
**方案**: D 频次共识 (5 天 window, top 10), 5 天持有

## 总览

| 指标 | 值 |
|---|---:|
| 周数 | {n_weeks} |
| picks 总数 | {n_picks} |
| 胜率 (5d ret > 0) | **{win_rate * 100:.1f}%** |
| 平均 5d 收益 | {avg_ret * 100:+.2f}% |
| 平均 5d alpha (vs 池等权) | **{avg_alpha * 100:+.2f}pp** |
| 累计 alpha (复利) | **{cum_alpha.iloc[-1] * 100:+.2f}%** |
| 累计绝对收益 (复利) | {cum_ret.iloc[-1] * 100:+.2f}% |

## Per-Pick 统计 (portfolio-analytics.trade_statistics)

| 指标 | 值 |
|---|---:|
| 总 picks | {stats['total_trades']} |
| 胜率 | {stats['win_rate'] * 100:.1f}% |
| 平均盈利 | {stats['avg_win'] * 100:+.2f}% |
| 平均亏损 | {stats['avg_loss'] * 100:+.2f}% |
| 最大盈利 | {stats['largest_win'] * 100:+.2f}% |
| 最大亏损 | {stats['largest_loss'] * 100:+.2f}% |
| Profit Factor | {stats['profit_factor']:.2f} |
| Expectancy (期望) | {stats['expectancy'] * 100:+.2f}% |

## 周度表现

| 周一 | picks | 等权 5d | bench 5d | alpha | 胜数 |
|---|---:|---:|---:|---:|---:|
"""
    for _, w in weekly.iterrows():
        md += (f"| {w['monday']} | {int(w['n_picks'])} | "
               f"{w['avg_ret'] * 100:+.2f}% | {w['bench'] * 100:+.2f}% | "
               f"{w['alpha'] * 100:+.2f}% | {int(w['win_n'])}/{int(w['n_picks'])} |\n")

    md += f"""
**累计 alpha 曲线**: {' → '.join(f'{x * 100:+.1f}%' for x in cum_alpha)}

## 按排名 (rank 1 = 当周共识最高)

| rank | n | 胜率 | avg ret | avg alpha |
|---:|---:|---:|---:|---:|
"""
    for rank, row in by_rank.iterrows():
        md += (f"| {rank} | {int(row['n'])} | {row['win_rate'] * 100:.0f}% | "
               f"{row['avg_ret'] * 100:+.2f}% | {row['avg_alpha'] * 100:+.2f}% |\n")

    md += "\n## Top 10 赢家\n\n| 周一 | rank | code | freq | 5d ret | alpha |\n|---|---:|:---|---:|---:|---:|\n"
    for _, w in top_winners.iterrows():
        md += (f"| {w['monday']} | {int(w['rank'])} | {w['code']} | {int(w['freq'])} | "
               f"{w['ret_5d'] * 100:+.2f}% | {w['alpha'] * 100:+.2f}% |\n")

    md += "\n## Top 10 输家\n\n| 周一 | rank | code | freq | 5d ret | alpha |\n|---|---:|:---|---:|---:|---:|\n"
    for _, w in top_losers.iterrows():
        md += (f"| {w['monday']} | {int(w['rank'])} | {w['code']} | {int(w['freq'])} | "
               f"{w['ret_5d'] * 100:+.2f}% | {w['alpha'] * 100:+.2f}% |\n")

    md += f"""
## 解读

- **胜率 {win_rate * 100:.0f}%**: {'高于 50% — 模型有正向 edge' if win_rate > 0.5 else '低于 50% — 模型胜率不足，但 expectancy 仍可能为正（赢多输少）'}
- **Profit Factor {stats['profit_factor']:.2f}**: {'>1.5 优秀' if stats['profit_factor'] > 1.5 else '>1 仍盈利' if stats['profit_factor'] > 1 else '<1 整体亏损'}
- **Expectancy {stats['expectancy'] * 100:+.2f}%**: 单只 pick 的期望收益（含输赢概率加权）
- **Rank-by-Rank**: rank 1 (最高共识) 胜率 / 收益是否显著高于 rank 10？这反映共识分数是否真有 alpha

## 局限

- 13 周样本仍小（quant-analyst 已指出 paired t p > 0.05）
- 未含交易成本（实盘换手 ~30%/周）
- bench = 池等权（5e8-5e9 小盘），与上证/沪深 300 表现可能差异大
- pick 不区分行业/板块；行业集中度风险未呈现
"""
    Path(out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(out_md).write_text(md)


if __name__ == "__main__":
    sys.exit(main())
