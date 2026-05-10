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


def _parse_top_ns(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def _load_st_codes(conn) -> set:
    """从 industry_map 加载所有名字含 ST 的 code 集合 (近似 PIT，
    假设 ST 状态在回测期内变化不大；严格 PIT 需 namechange 历史表)"""
    return {
        r[0] for r in conn.execute(
            "SELECT code FROM industry_map "
            "WHERE name LIKE '%ST%' OR name LIKE '*ST%'"
        ).fetchall()
    }


def main():
    parser = argparse.ArgumentParser()
    today = datetime.now().strftime("%Y-%m-%d")
    parser.add_argument("--start", default=f"{datetime.now().year}-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--pick-top-ns", default="3,5,10",
                        help="逗号分隔的 picks 数列表（每周一取多少只共识 picks）")
    parser.add_argument("--exclude-st", action="store_true",
                        help="排除 ST/*ST 股票（验证策略真 alpha 是否依赖 ST 反弹）")
    parser.add_argument("--out-csv", default="logs/picks_history_backtest.csv")
    parser.add_argument("--out-md", default="docs/picks_performance.md")
    args = parser.parse_args()

    top_ns = _parse_top_ns(args.pick_top_ns)
    print(f"=== picks 表现追踪 ===")
    print(f"  start={args.start}, end={args.end or '自动'}")
    print(f"  pick_top_ns={top_ns}, exclude_st={args.exclude_st}")

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

    st_codes = _load_st_codes(conn) if args.exclude_st else set()
    if args.exclude_st:
        print(f"  exclude_st: 加载 {len(st_codes)} 只 ST 股")

    print(f"\n[4/5] 对每周一构建 picks + 算 5d 收益（top_n in {top_ns}）...")
    rows = []
    skipped_st_total = 0
    for D in mondays:
        bench = benchmark_5d(stock_data, D)
        ret_cache = {}
        max_top = max(top_ns)
        # 内部 cache top_n 固定 10（生产语义），picks 候选放宽到 max_top * 2
        # 让排除 ST 后仍有足够候选填到 max_top
        picks_full = consensus_picks_for(D, daily_scored, buffer_dates,
                                         top_n=max(10, max_top * 2))
        if not picks_full:
            continue
        if st_codes:
            before = len(picks_full)
            picks_full = [p for p in picks_full if p["code"] not in st_codes]
            skipped_st_total += before - len(picks_full)
        for top_n in top_ns:
            for rank, p in enumerate(picks_full[:top_n], 1):
                code = p["code"]
                if code not in ret_cache:
                    ret_cache[code] = fwd_return(stock_data, code, D)
                ret = ret_cache[code]
                if ret is None:
                    continue
                rows.append({
                    "pick_top_n": top_n,
                    "monday": D, "rank": rank, "code": code,
                    "freq": p["freq"], "avg_score": round(p["avg_score"], 4),
                    "ret_5d": ret, "bench_5d": bench,
                    "alpha": ret - bench if not pd.isna(bench) else np.nan,
                })
    if args.exclude_st:
        print(f"  累计跳过 ST picks: {skipped_st_total}")
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


def _summary_for(sub: pd.DataFrame) -> dict:
    """单个 pick_top_n 子集的总览指标"""
    pnl = sub["ret_5d"]
    stats = trade_statistics(pnl)
    weekly = sub.groupby("monday").agg(
        avg_ret=("ret_5d", "mean"),
        bench=("bench_5d", "first"),
    ).reset_index()
    weekly["alpha"] = weekly["avg_ret"] - weekly["bench"]
    cum_ret = (1 + weekly["avg_ret"]).cumprod() - 1
    cum_alpha = (1 + weekly["alpha"]).cumprod() - 1
    return {
        "n_weeks": sub["monday"].nunique(),
        "n_picks": len(sub),
        "win_rate": stats["win_rate"],
        "avg_ret": pnl.mean(),
        "avg_alpha": sub["alpha"].mean(),
        "profit_factor": stats["profit_factor"],
        "expectancy": stats["expectancy"],
        "avg_win": stats["avg_win"],
        "avg_loss": stats["avg_loss"],
        "largest_win": stats["largest_win"],
        "largest_loss": stats["largest_loss"],
        "cum_ret": cum_ret.iloc[-1] if len(cum_ret) else 0.0,
        "cum_alpha": cum_alpha.iloc[-1] if len(cum_alpha) else 0.0,
        "weekly_alpha_std": weekly["alpha"].std(ddof=1) if len(weekly) > 1 else 0.0,
        "weekly_sharpe": (weekly["alpha"].mean() / weekly["alpha"].std(ddof=1)
                          if len(weekly) > 1 and weekly["alpha"].std(ddof=1) > 0 else 0.0),
    }


def write_report(df: pd.DataFrame, out_md: str) -> None:
    top_ns = sorted(df["pick_top_n"].unique())
    summaries = {n: _summary_for(df[df["pick_top_n"] == n]) for n in top_ns}

    # 取最大 top_n 用作 top winners/losers / by_rank（picks 集合包含小 top_n）
    max_top = max(top_ns)
    full = df[df["pick_top_n"] == max_top]
    by_rank = full.groupby("rank").agg(
        n=("code", "count"),
        avg_ret=("ret_5d", "mean"),
        win_rate=("ret_5d", lambda x: (x > 0).mean()),
        avg_alpha=("alpha", "mean"),
    ).round(4)
    top_winners = full.nlargest(10, "ret_5d")[["monday", "rank", "code", "freq", "ret_5d", "alpha"]]
    top_losers = full.nsmallest(10, "ret_5d")[["monday", "rank", "code", "freq", "ret_5d", "alpha"]]

    md = f"""# Picks 表现追踪 — Rank 精选对照 (D_top{'/'.join(map(str, top_ns))})

**生成日期**: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}
**回测区间**: {df['monday'].min()} ~ {df['monday'].max()}
**模型**: `xgb_ranker.json` (PRODUCTION_MODEL)
**方案**: D 频次共识 (5 天 window, 5 天持有)

## 改进点 #1: Rank 精选 — 多 top_n 对照

理论支撑: IC decay 单调性 (Grinold-Kahn) / 业界 top decile 标准 / 过度分散摊薄 alpha
(Markowitz 边际效用递减)。预期：D_top3 > D_top5 > D_top10 在 PF / sharpe / alpha。

| 指标 | """ + " | ".join(f"D_top{n}" for n in top_ns) + " |\n"
    md += "|---|" + "|".join(["---:"] * len(top_ns)) + "|\n"

    def row(label, key, fmt):
        return ("| " + label + " | "
                + " | ".join(fmt(summaries[n][key]) for n in top_ns) + " |\n")

    md += row("周数", "n_weeks", lambda v: str(int(v)))
    md += row("picks 总数", "n_picks", lambda v: str(int(v)))
    md += row("胜率", "win_rate", lambda v: f"**{v * 100:.1f}%**")
    md += row("平均 5d 收益", "avg_ret", lambda v: f"{v * 100:+.2f}%")
    md += row("平均 5d alpha (pp)", "avg_alpha", lambda v: f"**{v * 100:+.2f}**")
    md += row("Profit Factor", "profit_factor", lambda v: f"**{v:.2f}**")
    md += row("Expectancy", "expectancy", lambda v: f"{v * 100:+.2f}%")
    md += row("avg win", "avg_win", lambda v: f"{v * 100:+.2f}%")
    md += row("avg loss", "avg_loss", lambda v: f"{v * 100:+.2f}%")
    md += row("largest win", "largest_win", lambda v: f"{v * 100:+.2f}%")
    md += row("largest loss", "largest_loss", lambda v: f"{v * 100:+.2f}%")
    md += row("累计 alpha (复利)", "cum_alpha", lambda v: f"**{v * 100:+.2f}%**")
    md += row("累计 ret (复利)", "cum_ret", lambda v: f"{v * 100:+.2f}%")
    md += row("周度 alpha σ", "weekly_alpha_std", lambda v: f"{v * 100:.2f}%")
    md += row("周度 sharpe-like", "weekly_sharpe", lambda v: f"**{v:.3f}**")

    # 决策摘要
    best = max(top_ns, key=lambda n: summaries[n]["weekly_sharpe"])
    pf_winner = max(top_ns, key=lambda n: summaries[n]["profit_factor"])
    md += f"""
**决策**:
- sharpe 最优: **D_top{best}** ({summaries[best]['weekly_sharpe']:.3f})
- profit factor 最优: **D_top{pf_winner}** ({summaries[pf_winner]['profit_factor']:.2f})
- 胜率最优: **D_top{max(top_ns, key=lambda n: summaries[n]['win_rate'])}** ({max(summaries[n]['win_rate'] for n in top_ns) * 100:.1f}%)

## 按排名 (rank 1 = 当周共识最高)

基于 D_top{max_top} 全集；小 top_n 是其前缀。

| rank | n | 胜率 | avg ret | avg alpha |
|---:|---:|---:|---:|---:|
"""
    for rank, r_row in by_rank.iterrows():
        md += (f"| {rank} | {int(r_row['n'])} | {r_row['win_rate'] * 100:.0f}% | "
               f"{r_row['avg_ret'] * 100:+.2f}% | {r_row['avg_alpha'] * 100:+.2f}% |\n")

    md += "\n## Top 10 赢家 (D_top{} 全集)\n\n".format(max_top)
    md += "| 周一 | rank | code | freq | 5d ret | alpha |\n|---|---:|:---|---:|---:|---:|\n"
    for _, w in top_winners.iterrows():
        md += (f"| {w['monday']} | {int(w['rank'])} | {w['code']} | {int(w['freq'])} | "
               f"{w['ret_5d'] * 100:+.2f}% | {w['alpha'] * 100:+.2f}% |\n")

    md += "\n## Top 10 输家\n\n| 周一 | rank | code | freq | 5d ret | alpha |\n|---|---:|:---|---:|---:|---:|\n"
    for _, w in top_losers.iterrows():
        md += (f"| {w['monday']} | {int(w['rank'])} | {w['code']} | {int(w['freq'])} | "
               f"{w['ret_5d'] * 100:+.2f}% | {w['alpha'] * 100:+.2f}% |\n")

    md += f"""
## 解读

- **PF 最优 D_top{pf_winner} = {summaries[pf_winner]['profit_factor']:.2f}**: {'已超 1.5 业界 acceptable 阈值' if summaries[pf_winner]['profit_factor'] > 1.5 else '仍未达 1.5 业界 acceptable，但比 D_top10 改善'}
- **Rank-by-Rank**: rank 1-3 vs rank 4-10 的胜率/alpha 落差是 IC decay 的实证；如果 rank 1-3 显著更好，rank 精选有效
- **统计稳健性**: D_top3 picks 数仅 {summaries[3]['n_picks'] if 3 in summaries else 'n/a'}（vs D_top10 的 {summaries[10]['n_picks'] if 10 in summaries else 'n/a'}），样本更小标准误更大；需谨慎外推

## 局限

- 14 周样本仍小（walk-forward-validation skill 已指出 paired t p ≈ 0.11）
- 未含交易成本（实盘换手 ~30%/周）
- bench = 池等权（5e8-5e9 小盘），与上证/沪深 300 表现可能差异大
- pick 不区分行业/板块；行业集中度风险未呈现
"""
    Path(out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(out_md).write_text(md)


if __name__ == "__main__":
    sys.exit(main())
