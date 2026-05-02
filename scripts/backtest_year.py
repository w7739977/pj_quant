"""今年以来 D 方案 vs 日频对比回测

默认窗口: 当年 1/1 到今天前 5 个交易日（留 forward return 空间）
权重: 50/50 ML/因子（与生产一致）

四方案:
  A. 日频基线 — 每天 top 10，5d 持有
  B. 周一快照 — 周一 top 10，5d 持有
  C. 5天信号平均 — 周一选股，前 5 天 final_score 均值
  D. 5天频次共识 (生产) — 周一选股，前 5 天 top 10 频次共识

用法:
  python3 scripts/backtest_year.py                       # 默认今年到今天-5d
  python3 scripts/backtest_year.py --start 2026-01-01    # 自定义起点
  python3 scripts/backtest_year.py --top-n 10            # 选股数
"""
import os
import sys
import sqlite3
import argparse
import warnings
import logging
from datetime import datetime
from collections import Counter

warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.WARNING)

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

TOP_N = 10
HOLD = 5
LATEST_FOR_POOL = "2026-04-25"  # 池子要求数据日期 ≥ 此日（剔除停牌）


def main():
    parser = argparse.ArgumentParser()
    today = datetime.now().strftime("%Y-%m-%d")
    parser.add_argument("--start", default=None, help="回测起点 YYYY-MM-DD（默认当年 01-01）")
    parser.add_argument("--end", default=None, help="回测终点 YYYY-MM-DD（默认今天前 5 个交易日）")
    parser.add_argument("--top-n", type=int, default=TOP_N, help="每次选股数")
    parser.add_argument("--out", default="logs/backtest_year.csv", help="结果 CSV 路径")
    args = parser.parse_args()

    year = datetime.now().year
    start = args.start or f"{year}-01-01"
    print(f"=== 今年以来回测 (start={start}, end={args.end or '自动'}, top_n={args.top_n}) ===")

    print("\n[1/5] 初始化...")
    r._FIN_CACHE = load_all_pit_to_dict()
    if not os.path.exists(PRODUCTION_MODEL):
        print(f"❌ 模型不存在: {PRODUCTION_MODEL}")
        print(f"先 python3 main.py evolve")
        return 1
    model = XGBRegressor()
    model.load_model(PRODUCTION_MODEL)

    # buffer 区，让早期周一也有 5 天 prior
    conn = sqlite3.connect("data/quant.db")
    all_dates_after_start = [str(row[0])[:10] for row in conn.execute(
        "SELECT DISTINCT date FROM stock_000001 WHERE date >= ? ORDER BY date",
        (f"{int(year)-1}-12-15",),  # 多取 2 周 buffer
    ).fetchall()]
    # end 默认: 今天前 5 个交易日
    if args.end:
        end_date = args.end
    else:
        future_dates = [d for d in all_dates_after_start if d > today]
        if len(future_dates) >= 5:
            end_date = future_dates[4]
        else:
            today_dates = sorted([d for d in all_dates_after_start if d <= today])
            end_date = today_dates[-6] if len(today_dates) >= 6 else today_dates[-1]

    decision_dates = [d for d in all_dates_after_start if start <= d <= end_date]
    print(f"  全交易日: {len(all_dates_after_start)} (含 buffer)")
    print(f"  决策日 ({start} ~ {end_date}): {len(decision_dates)}")

    pool = get_small_cap_stocks(5e8, 5e9)
    print(f"  股票池: {len(pool)} 只 (剔除北交所)")

    print("\n[2/5] 预加载日线...")
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
    print(f"  {len(stock_data)} 只可用")

    def fwd_return(code, D, hold):
        df = stock_data.get(code)
        if df is None: return None
        before = df[df["date_str"] <= D].tail(1)
        after = df[df["date_str"] > D].head(hold)
        if len(before) == 1 and len(after) >= hold:
            p0 = before.iloc[0]["close"]; p1 = after.iloc[hold-1]["close"]
            if p0 > 0: return p1/p0 - 1
        return None

    def benchmark(D, hold):
        rs = [fwd_return(c, D, hold) for c in stock_data]
        rs = [x for x in rs if x is not None]
        return np.mean(rs) if rs else np.nan

    # 计算每日 scored
    print(f"\n[3/5] 算每日 final_score（{len(all_dates_after_start)} 个工作日）...")
    daily_scored = {}
    buffer_dates = [d for d in all_dates_after_start if d <= end_date]
    for i, D in enumerate(buffer_dates, 1):
        if i % 10 == 0:
            print(f"  [{D}] {i}/{len(buffer_dates)}")
        rows = []
        for sym, df in stock_data.items():
            win = df[df["date_str"] <= D].tail(120)
            if len(win) < 20:
                continue
            f = {"code": sym}
            f.update(calc_momentum(win)); f.update(calc_volatility(win))
            f.update(calc_turnover_factor(win)); f.update(calc_volume_price(win))
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
        daily_scored[D] = scored[["code", "final_score"]]

    mondays = [d for d in decision_dates
               if datetime.strptime(d, "%Y-%m-%d").weekday() == 0]
    print(f"\n[4/5] 跑 4 方案回测...")
    print(f"  周一: {len(mondays)} 个")

    results = []

    # A. 日频
    for D in decision_dates:
        if D not in daily_scored: continue
        top = daily_scored[D].sort_values("final_score", ascending=False).head(args.top_n)
        rets = [fwd_return(c, D, HOLD) for c in top["code"]]
        rets = [x for x in rets if x is not None]
        if len(rets) < args.top_n // 2: continue
        bench = benchmark(D, HOLD)
        if not (bench == bench): continue
        results.append({"method": "A 日频", "date": D,
                        "ret": np.mean(rets), "bench": bench,
                        "alpha": np.mean(rets) - bench,
                        "win": sum(1 for x in rets if x > 0), "n": len(rets)})

    # B. 周一快照
    for D in mondays:
        if D not in daily_scored: continue
        top = daily_scored[D].sort_values("final_score", ascending=False).head(args.top_n)
        rets = [fwd_return(c, D, HOLD) for c in top["code"]]
        rets = [x for x in rets if x is not None]
        if len(rets) < args.top_n // 2: continue
        bench = benchmark(D, HOLD)
        if not (bench == bench): continue
        results.append({"method": "B 周一快照", "date": D,
                        "ret": np.mean(rets), "bench": bench,
                        "alpha": np.mean(rets) - bench,
                        "win": sum(1 for x in rets if x > 0), "n": len(rets)})

    # C. 5 天信号平均
    for D in mondays:
        idx = buffer_dates.index(D)
        if idx < 5: continue
        prev = buffer_dates[idx-5:idx]
        score_sum = {}
        for pd_ in prev:
            if pd_ not in daily_scored: continue
            for _, row in daily_scored[pd_].iterrows():
                score_sum.setdefault(row["code"], []).append(row["final_score"])
        avg = {c: np.mean(s) for c, s in score_sum.items() if len(s) >= 3}
        picks = [c for c, _ in sorted(avg.items(), key=lambda x: -x[1])[:args.top_n]]
        rets = [fwd_return(c, D, HOLD) for c in picks]
        rets = [x for x in rets if x is not None]
        if len(rets) < args.top_n // 2: continue
        bench = benchmark(D, HOLD)
        if not (bench == bench): continue
        results.append({"method": "C 5天平均", "date": D,
                        "ret": np.mean(rets), "bench": bench,
                        "alpha": np.mean(rets) - bench,
                        "win": sum(1 for x in rets if x > 0), "n": len(rets)})

    # D. 5 天频次共识
    for D in mondays:
        idx = buffer_dates.index(D)
        if idx < 5: continue
        prev = buffer_dates[idx-5:idx]
        counter = Counter()
        score_sum = {}
        for pd_ in prev:
            if pd_ not in daily_scored: continue
            top = daily_scored[pd_].sort_values("final_score", ascending=False).head(args.top_n)
            for _, row in top.iterrows():
                counter[row["code"]] += 1
                score_sum.setdefault(row["code"], []).append(row["final_score"])
        ranked = sorted(counter.items(),
                        key=lambda x: (-x[1], -float(np.mean(score_sum[x[0]]))))
        picks = [c for c, _ in ranked[:args.top_n]]
        rets = [fwd_return(c, D, HOLD) for c in picks]
        rets = [x for x in rets if x is not None]
        if len(rets) < args.top_n // 2: continue
        bench = benchmark(D, HOLD)
        if not (bench == bench): continue
        results.append({"method": "D 频次共识(生产)", "date": D,
                        "ret": np.mean(rets), "bench": bench,
                        "alpha": np.mean(rets) - bench,
                        "win": sum(1 for x in rets if x > 0), "n": len(rets)})

    # 汇总
    res = pd.DataFrame(results)
    if res.empty:
        print("\n❌ 无有效观测点")
        return 1

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    res.to_csv(args.out, index=False)

    print(f"\n[5/5] 汇总结果...")
    print("\n" + "=" * 95)
    print("=== 按方案汇总 ===")
    agg = res.groupby("method").agg(
        n=("date", "count"),
        avg_alpha=("alpha", "mean"),
        median_alpha=("alpha", "median"),
        alpha_std=("alpha", "std"),
        beat_bench=("alpha", lambda x: (x > 0).mean()),
    ).round(4)
    agg["sharpe_like"] = (agg["avg_alpha"] / agg["alpha_std"]).round(3)
    agg["winrate"] = res.groupby("method").apply(
        lambda x: x["win"].sum() / x["n"].sum()
    ).round(4)
    print(agg.to_string())

    print("\n=== 累计 alpha (周频，简单复利) ===")
    for method in ["B 周一快照", "C 5天平均", "D 频次共识(生产)"]:
        s = res[res["method"] == method].sort_values("date")
        if len(s) < 2: continue
        cum = (s["alpha"] + 1).cumprod() - 1
        print(f"  {method}: {len(s)} 周累计 = {cum.iloc[-1]*100:+.2f}%")

    print(f"\n保存: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
