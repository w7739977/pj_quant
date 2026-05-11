"""3d 方案 vs d 方案回测对照 (2025-01-01 至今 ~64 周)

唯一变量: 推送频次 (d = 每周一推 / 3d = 每周一、三、五各推一次)。
其他全部一致: top 10 / 5d hold / 5d window / 池 5-50 亿 / 节假日跳过。

视角: 信号质量评估 (每次推送独立计 5d 表现)，不模拟持仓重叠或换仓。

用法:
  python3 scripts/backtest_3d_vs_d.py
  python3 scripts/backtest_3d_vs_d.py --start 2025-06-01
"""
import argparse
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

import logging
logging.basicConfig(level=logging.WARNING)

import numpy as np
import pandas as pd
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
from portfolio.consensus import is_window_fresh

TOP_N = 10
HOLD = 5
WINDOW = 5


def build_daily_scored(model, stock_data, dates):
    daily = {}
    for i, D in enumerate(dates, 1):
        if i % 50 == 0:
            print(f"    [{D}] {i}/{len(dates)}")
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


def benchmark_5d(stock_data, D):
    rs = [fwd_return(stock_data, c, D) for c in stock_data]
    rs = [x for x in rs if x is not None]
    return float(np.mean(rs)) if rs else float("nan")


def consensus_picks_for(D, daily_scored, buffer_dates, window=WINDOW, top_n=TOP_N):
    if D not in buffer_dates:
        return []
    idx = buffer_dates.index(D)
    if idx < window:
        return []
    prev = buffer_dates[idx - window: idx]
    counter: Counter = Counter()
    score_sum: dict = {}
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


def main():
    parser = argparse.ArgumentParser()
    today = datetime.now().strftime("%Y-%m-%d")
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default=None)
    args = parser.parse_args()

    print(f"=== 3d vs d 方案回测对照 ===")
    print(f"  start={args.start}, end={args.end or '自动'}")

    print("\n[1/5] 加载财务 + 模型 + 池...")
    r._FIN_CACHE = load_all_pit_to_dict()
    model = XGBRegressor()
    model.load_model(PRODUCTION_MODEL)
    pool = get_small_cap_stocks(5e8, 5e9)
    print(f"  pool: {len(pool)} 只")

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
        stock_data[sym] = df
    print(f"  stock_data: {len(stock_data)} 只 / 决策日 {len(decision_dates)}")

    print(f"\n[3/5] 算每日 final_score ({len(buffer_dates)} 日)...")
    daily_scored = build_daily_scored(model, stock_data, buffer_dates)
    print(f"  daily_scored: {len(daily_scored)} 天")

    d_pushes = [d for d in decision_dates
                if datetime.strptime(d, "%Y-%m-%d").weekday() == 0]
    d3_pushes = [d for d in decision_dates
                 if datetime.strptime(d, "%Y-%m-%d").weekday() in (0, 2, 4)]
    print(f"  d 方案推送日: {len(d_pushes)} 个 (仅周一)")
    print(f"  3d 方案推送日: {len(d3_pushes)} 个 (周一/三/五)")

    print(f"\n[4/5] 对每个推送日构建 picks + 算 5d 收益...")
    rows = []
    bench_cache: dict = {}

    def push_day_eval(D, method):
        picks = consensus_picks_for(D, daily_scored, buffer_dates)
        if not picks:
            return []
        if D not in bench_cache:
            bench_cache[D] = benchmark_5d(stock_data, D)
        bench = bench_cache[D]
        if pd.isna(bench):
            return []
        out = []
        for rank, p in enumerate(picks, 1):
            ret = fwd_return(stock_data, p["code"], D)
            if ret is None:
                continue
            out.append({
                "method": method, "push_date": D, "rank": rank,
                "code": p["code"], "freq": p["freq"],
                "avg_score": round(p["avg_score"], 4),
                "ret_5d": ret, "bench_5d": bench,
                "alpha": ret - bench,
            })
        return out

    print("  跑 d 方案 (周一)...")
    for i, D in enumerate(d_pushes, 1):
        rows.extend(push_day_eval(D, "d"))
        if i % 10 == 0:
            print(f"    [{D}] {i}/{len(d_pushes)}")

    print("  跑 3d 方案 (周一/三/五)...")
    for i, D in enumerate(d3_pushes, 1):
        # 周一的 picks 跟 d 一致，复用结果可省一半时间，但这里求简单不优化
        rows.extend(push_day_eval(D, "3d"))
        if i % 30 == 0:
            print(f"    [{D}] {i}/{len(d3_pushes)}")

    df = pd.DataFrame(rows)
    out_csv = "logs/backtest_3d_vs_d.csv"
    os.makedirs("logs", exist_ok=True)
    df.to_csv(out_csv, index=False)
    print(f"\n[5/5] 生成报告...")
    print(f"  CSV: {out_csv} ({len(df)} 行)")

    write_report(df, "docs/backtest_3d_vs_d.md")
    print(f"  MD: docs/backtest_3d_vs_d.md")


def write_report(df: pd.DataFrame, out_md: str) -> None:
    sys.path.insert(0, str(REPO / ".claude/skills/portfolio-analytics/scripts"))
    from analyze_portfolio import trade_statistics  # noqa

    def stats_for(sub, label):
        if sub.empty:
            return None
        pnl = sub["ret_5d"]
        ts = trade_statistics(pnl)
        # 关键: 按 ISO 周聚合 (不是按 push_date)。3d 一周三次推送会被合并取均，
        # 避免 cumprod 期数不公平 (d=每周 1 期, 3d=每周 3 期 → 复利 artifact)
        sub_w = sub.copy()
        sub_w["week"] = pd.to_datetime(sub_w["push_date"]).dt.to_period("W").astype(str)
        weekly_alpha = sub_w.groupby("week")["alpha"].mean()
        cum_a = ((1 + weekly_alpha).cumprod() - 1).iloc[-1] if len(weekly_alpha) else 0
        return {
            "label": label,
            "n_weeks": len(weekly_alpha),
            "n_pushes": sub["push_date"].nunique(),
            "n_picks": len(sub),
            "win_rate": ts["win_rate"],
            "avg_ret": pnl.mean(),
            "avg_alpha": sub["alpha"].mean(),
            "pf": ts["profit_factor"],
            "expectancy": ts["expectancy"],
            "cum_alpha": cum_a,
            "weekly_alpha_std": float(weekly_alpha.std(ddof=1)) if len(weekly_alpha) > 1 else 0,
        }

    d = stats_for(df[df["method"] == "d"], "d")
    d3 = stats_for(df[df["method"] == "3d"], "3d")

    # 排 ST 子集
    sys.path.insert(0, str(REPO))
    conn = sqlite3.connect("data/quant.db")
    st_codes = {
        r[0] for r in conn.execute(
            "SELECT code FROM industry_map WHERE name LIKE '%ST%' OR name LIKE '*ST%'"
        )
    }
    conn.close()
    df_no_st = df[~df["code"].astype(str).str.zfill(6).isin(st_codes)]
    d_ns = stats_for(df_no_st[df_no_st["method"] == "d"], "d 排ST")
    d3_ns = stats_for(df_no_st[df_no_st["method"] == "3d"], "3d 排ST")

    # 配对 t test (同周对比)
    import numpy as np
    from scipy.stats import ttest_1samp
    df_w = df.copy()
    df_w["week"] = pd.to_datetime(df_w["push_date"]).dt.to_period("W").astype(str)
    d_week = df_w[df_w["method"] == "d"].groupby("week")["alpha"].mean()
    d3_week = df_w[df_w["method"] == "3d"].groupby("week")["alpha"].mean()
    common = sorted(set(d_week.index) & set(d3_week.index))
    diffs = np.array([d3_week[w] - d_week[w] for w in common])
    t_stat, p_value = (0.0, 1.0)
    if len(diffs) > 1:
        t_stat, p_value = ttest_1samp(diffs, 0)
    md = f"""# 3d 方案 vs d 方案回测对照 — 阶段 1 信号质量评估

**生成日期**: {datetime.now().strftime('%Y-%m-%d %H:%M')}
**回测区间**: {df['push_date'].min()} ~ {df['push_date'].max()}
**模型**: `xgb_ranker.json` (PRODUCTION_MODEL)
**池**: 5-50 亿小盘股 (~1700 只)
**框架层**: L2 (信号质量评估，见 `VALIDATION_PHILOSOPHY.md`)

## 唯一变量

| 维度 | d 方案 (生产) | 3d 方案 (新提案) |
|---|---|---|
| 推送频次 | 每周一 | **每周一、三、五** |
| 每次推 | top 10 | top 10 |
| Hold | 5 工作日 | 5 工作日 |
| Window | 5 工作日 cache | 5 工作日 cache |
| 节假日 | 跳过 | 跳过 |

> **⚠️ 信号视角，不是实盘 PnL**：每次推送独立计 5d 收益，不模拟持仓重叠或换仓建议。生产实盘仍是动态 hold (止损/止盈/超时)。本报告检验"频次升高对信号质量的影响"。

## 总览对照

| 指标 | d (周一) | 3d (周一/三/五) | 3d 优势 | 怎么读 |
|---|---:|---:|---:|---|
| 推送次数 | {d['n_pushes']} | {d3['n_pushes']} | ×{d3['n_pushes']/d['n_pushes']:.1f} | 3d 频次是 d 的 ~3 倍 |
| picks 总数 | {d['n_picks']} | {d3['n_picks']} | ×{d3['n_picks']/d['n_picks']:.1f} | 同上 |
| 胜率 | {d['win_rate']*100:.1f}% | {d3['win_rate']*100:.1f}% | {(d3['win_rate']-d['win_rate'])*100:+.1f}pp | 50% 是 coin flip |
| 平均 5d α | {d['avg_alpha']*100:+.2f}pp | {d3['avg_alpha']*100:+.2f}pp | {(d3['avg_alpha']-d['avg_alpha'])*100:+.2f}pp | 单只 picks 平均跑赢基准 |
| Profit Factor | {d['pf']:.2f} | {d3['pf']:.2f} | {d3['pf']-d['pf']:+.2f} | 1.5 合格，2.0 优秀 |
| Expectancy | {d['expectancy']*100:+.2f}% | {d3['expectancy']*100:+.2f}% | {(d3['expectancy']-d['expectancy'])*100:+.2f}pp | 单只期望 |
| 累计 α (复利) | {d['cum_alpha']*100:+.2f}% | {d3['cum_alpha']*100:+.2f}% | {(d3['cum_alpha']-d['cum_alpha'])*100:+.2f}pp | 周度 α 复利累乘 |
| 周度 α σ | {d['weekly_alpha_std']*100:.2f}% | {d3['weekly_alpha_std']*100:.2f}% | — | 波动性 |

## 排除 ST 后对照 (真 alpha 视角)

| 指标 | d 排 ST | 3d 排 ST | 3d 优势 |
|---|---:|---:|---:|
| picks 数 | {d_ns['n_picks'] if d_ns else 'n/a'} | {d3_ns['n_picks'] if d3_ns else 'n/a'} | — |
| 胜率 | {d_ns['win_rate']*100 if d_ns else 0:.1f}% | {d3_ns['win_rate']*100 if d3_ns else 0:.1f}% | {((d3_ns['win_rate']-d_ns['win_rate'])*100) if d_ns and d3_ns else 0:+.1f}pp |
| 平均 5d α | {d_ns['avg_alpha']*100 if d_ns else 0:+.2f}pp | {d3_ns['avg_alpha']*100 if d3_ns else 0:+.2f}pp | {((d3_ns['avg_alpha']-d_ns['avg_alpha'])*100) if d_ns and d3_ns else 0:+.2f}pp |
| Profit Factor | {d_ns['pf'] if d_ns else 0:.2f} | {d3_ns['pf'] if d3_ns else 0:.2f} | {(d3_ns['pf']-d_ns['pf']) if d_ns and d3_ns else 0:+.2f} |
| 累计 α | {d_ns['cum_alpha']*100 if d_ns else 0:+.2f}% | {d3_ns['cum_alpha']*100 if d3_ns else 0:+.2f}% | {((d3_ns['cum_alpha']-d_ns['cum_alpha'])*100) if d_ns and d3_ns else 0:+.2f}pp |

## 配对 t 检验 (核心统计判断)

把 d 和 3d 在同周的 weekly α 配对 ({len(common)} 个共同周):

```
3d - d 周度 α 差均值: {diffs.mean() * 100:+.3f} pp
SE:                   {diffs.std(ddof=1) / np.sqrt(len(diffs)) * 100:.3f}% (n={len(diffs)})
t-statistic:          {t_stat:.3f}
p-value (双尾):       {p_value:.4f}
```

**判断**: {'✅ 显著 (p<0.05) - 3d 信号质量真比 d 好' if p_value < 0.05 else '❌ 不显著 (p≥0.05) - 拒不掉「3d 和 d 信号质量相同」零假设'}

## 解读

- **统计稳健性**: 共同周数 {len(common)} {'≥ 80 (足够)' if len(common) >= 80 else f'< 80 (DSR 显著阈值还差 {80 - len(common)} 周)'}
- **3d 是否值得做**: 看 paired t p-value (>0.05 = 信号层无 edge) + 排 ST 是否仍优 (含 ST 数字会被反弹机噪声放大)
"""
    Path(out_md).parent.mkdir(parents=True, exist_ok=True)
    Path(out_md).write_text(md)


if __name__ == "__main__":
    sys.exit(main())
