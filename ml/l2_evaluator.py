"""
L2 信号质量评估器 — 用于 evolve 闭环判定新模型是否上线

替代旧的 R² 判定 (auto_evolve.is_new_best). 跟 L2 评估口径完全对齐:
  - 共识 picks (5d window + top 10)
  - 5d hold, 周一推送
  - 排 ST 池
  - 累计 α (周聚合复利)

设计原则:
  - 单一指标: 排 ST 累计 α (用户决策, 2026-05-16)
  - 时间窗: 2024-01-01 起 (固定起点, 让历次 evolve 在同样窗口对照)
  - 不写报告 / CSV (那是 backtest_3d_vs_d.py 的职责)
"""
from __future__ import annotations
import sys
import sqlite3
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from xgboost import XGBRegressor

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from factors.calculator import (  # noqa: E402
    calc_momentum, calc_volatility, calc_turnover_factor,
    calc_volume_price, calc_technical, winsorize_cross_section,
)
from data.storage import load_stock_daily  # noqa: E402
from portfolio.consensus import is_window_fresh  # noqa: E402
from strategy.small_cap import SmallCapStrategy  # noqa: E402
import ml.ranker as r  # noqa: E402
from ml.ranker import FEATURE_COLS  # noqa: E402

logger = logging.getLogger(__name__)

L2_TOP_N = 10
L2_HOLD = 5
L2_WINDOW = 5
DEFAULT_START = "2024-01-01"


def _build_daily_scored(model: XGBRegressor, stock_data: dict, dates: list) -> dict:
    """每日截面打分 (复用 backtest_3d_vs_d 的核心逻辑)"""
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
        # 容错: 因子缺失时退化 (新模型可能需要 chip/ohlc 因子但当前 build 不算)
        missing = [c for c in FEATURE_COLS if c not in fdf.columns]
        if missing:
            for c in missing:
                fdf[c] = np.nan
        X = fdf[FEATURE_COLS].copy().fillna(fdf[FEATURE_COLS].median())
        fdf["pred"] = model.predict(X)
        sc = SmallCapStrategy()
        scored = sc._score_stocks(fdf).reset_index(drop=True)
        ml_norm = (scored["pred"] - scored["pred"].mean()) / (scored["pred"].std() + 1e-8)
        factor_norm = (scored["score"] - scored["score"].mean()) / (scored["score"].std() + 1e-8)
        scored["final_score"] = ml_norm * 0.5 + factor_norm * 0.5
        daily[D] = scored[["code", "final_score"]]
    return daily


def _consensus_picks(D: str, daily_scored: dict, buffer_dates: list,
                     window: int = L2_WINDOW, top_n: int = L2_TOP_N) -> list:
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
    ranked = sorted(counter.items(),
                    key=lambda x: (-x[1], -float(np.mean(score_sum[x[0]]))))
    return [c for c, _ in ranked[:top_n]]


def _fwd_return(df: pd.DataFrame, D: str, hold: int = L2_HOLD) -> Optional[float]:
    before = df[df["date_str"] <= D].tail(1)
    after = df[df["date_str"] > D].head(hold)
    if len(before) == 1 and len(after) >= hold:
        p0 = before.iloc[0]["close"]
        p1 = after.iloc[hold - 1]["close"]
        if p0 > 0:
            return p1 / p0 - 1
    return None


def _load_st_codes(db_path: str = "data/quant.db") -> set:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT code FROM industry_map WHERE name LIKE '%ST%' OR name LIKE '*ST%'"
        ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()


def evaluate_model_l2(
    model_path: str,
    pool_codes: list,
    start_date: str = DEFAULT_START,
    end_date: Optional[str] = None,
) -> dict:
    """
    跑 L2 mini-backtest, 返回排 ST 关键指标

    Parameters
    ----------
    model_path : str        XGBoost 模型路径
    pool_codes : list[str]  股票池 codes
    start_date : str        回测起点 (默认 2024-01-01)
    end_date   : str        回测终点 (默认本地数据最新 - 5 天)

    Returns
    -------
    dict: {
        cum_alpha_no_st, pf_no_st, win_rate_no_st,
        cum_alpha_all, pf_all, n_picks_no_st, n_weeks
    }
    全 nan 表示无法评估 (池 / 数据不足)
    """
    if r.__dict__.get("_FIN_CACHE") is None:
        from data.financial_indicator import load_all_pit_to_dict
        r._FIN_CACHE = load_all_pit_to_dict()

    model = XGBRegressor()
    model.load_model(model_path)

    # 预加载日线
    stock_data = {}
    for sym in pool_codes:
        df = load_stock_daily(sym)
        if df is None or df.empty:
            continue
        df = df.copy()
        df["date_str"] = df["date"].astype(str).str[:10]
        stock_data[sym] = df

    if not stock_data:
        logger.warning("L2 evaluator: stock_data 为空")
        return _nan_result()

    # 取交易日
    conn = sqlite3.connect("data/quant.db")
    try:
        prefix = f"{int(start_date[:4]) - 1}-12-15"
        all_dates = [str(row[0])[:10] for row in conn.execute(
            "SELECT DISTINCT date FROM stock_000001 WHERE date >= ? ORDER BY date",
            (prefix,),
        ).fetchall()]
    finally:
        conn.close()

    if end_date is None:
        # 默认: 本地数据最新 - 5 天 (确保 hold 5 天后有数据)
        max_local = max(df["date_str"].max() for df in stock_data.values())
        # 找 max_local 在 all_dates 里的位置, 退 5 个
        if max_local in all_dates:
            idx = all_dates.index(max_local)
            end_date = all_dates[max(0, idx - 5)]
        else:
            end_date = max_local

    decision_dates = [d for d in all_dates if start_date <= d <= end_date]
    buffer_dates = [d for d in all_dates if d <= end_date]

    if not decision_dates:
        logger.warning(f"L2 evaluator: 决策日为空 (start={start_date}, end={end_date})")
        return _nan_result()

    logger.info(f"L2 evaluator: {len(decision_dates)} 决策日, "
                f"{len(stock_data)} 股票, model={model_path}")

    daily_scored = _build_daily_scored(model, stock_data, buffer_dates)

    # 只看 d 方案 (周一)
    d_pushes = [d for d in decision_dates
                if datetime.strptime(d, "%Y-%m-%d").weekday() == 0]

    bench_cache: dict = {}

    def _bench(D):
        if D not in bench_cache:
            rs = [_fwd_return(stock_data[c], D) for c in stock_data]
            rs = [x for x in rs if x is not None]
            bench_cache[D] = float(np.mean(rs)) if rs else float("nan")
        return bench_cache[D]

    st_codes = _load_st_codes()

    rows = []
    for D in d_pushes:
        picks = _consensus_picks(D, daily_scored, buffer_dates)
        if not picks:
            continue
        bench = _bench(D)
        if pd.isna(bench):
            continue
        for code in picks:
            ret = _fwd_return(stock_data[code], D) if code in stock_data else None
            if ret is None:
                continue
            rows.append({"D": D, "code": code, "ret": ret, "bench": bench,
                         "alpha": ret - bench, "is_st": code in st_codes})

    if not rows:
        return _nan_result()

    df = pd.DataFrame(rows)
    df["week"] = pd.to_datetime(df["D"]).dt.to_period("W").astype(str)

    # 排 ST 累计 α (周度等权聚合, cumprod)
    df_ns = df[~df["is_st"]]
    if df_ns.empty:
        return _nan_result()

    weekly_alpha_ns = df_ns.groupby("week")["alpha"].mean()
    cum_alpha_ns = float((1 + weekly_alpha_ns).cumprod().iloc[-1] - 1)
    # PF / win rate 在 picks 层 (每只独立)
    win_rate_ns = float((df_ns["alpha"] > 0).mean())
    pos_sum_ns = df_ns.loc[df_ns["alpha"] > 0, "alpha"].sum()
    neg_sum_ns = abs(df_ns.loc[df_ns["alpha"] < 0, "alpha"].sum())
    pf_ns = float(pos_sum_ns / neg_sum_ns) if neg_sum_ns > 1e-8 else float("inf")

    # 含 ST 对照
    weekly_alpha_all = df.groupby("week")["alpha"].mean()
    cum_alpha_all = float((1 + weekly_alpha_all).cumprod().iloc[-1] - 1)
    pos_sum_all = df.loc[df["alpha"] > 0, "alpha"].sum()
    neg_sum_all = abs(df.loc[df["alpha"] < 0, "alpha"].sum())
    pf_all = float(pos_sum_all / neg_sum_all) if neg_sum_all > 1e-8 else float("inf")

    return {
        "cum_alpha_no_st": cum_alpha_ns,
        "pf_no_st": pf_ns,
        "win_rate_no_st": win_rate_ns,
        "cum_alpha_all": cum_alpha_all,
        "pf_all": pf_all,
        "n_picks_no_st": len(df_ns),
        "n_picks_all": len(df),
        "n_weeks": len(weekly_alpha_ns),
        "start_date": start_date,
        "end_date": end_date,
    }


def _nan_result() -> dict:
    return {
        "cum_alpha_no_st": float("nan"),
        "pf_no_st": float("nan"),
        "win_rate_no_st": float("nan"),
        "cum_alpha_all": float("nan"),
        "pf_all": float("nan"),
        "n_picks_no_st": 0,
        "n_picks_all": 0,
        "n_weeks": 0,
        "start_date": None,
        "end_date": None,
    }


def main():
    """CLI: 对比两个模型在同一池子的 L2 表现"""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="模型路径")
    ap.add_argument("--old-model", default=None, help="对照模型 (可选)")
    ap.add_argument("--start", default=DEFAULT_START)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO)

    # 用 cache fallback 构造池子
    from data.storage import list_cached_stocks
    pool_codes = []
    for sym in list_cached_stocks():
        df = load_stock_daily(sym)
        if df is None or df.empty or len(df) < 120:
            continue
        mv = df["total_mv"].dropna()
        if mv.empty:
            continue
        cap = float(mv.iloc[-1]) * 1e4
        if 5e8 <= cap <= 5e9:
            pool_codes.append(sym)
    print(f"pool: {len(pool_codes)} 只")

    print(f"\n>>> 跑新模型 {args.model}")
    new = evaluate_model_l2(args.model, pool_codes, args.start, args.end)
    print(f"  cum_alpha_no_st: {new['cum_alpha_no_st']*100:+.2f}%")
    print(f"  pf_no_st: {new['pf_no_st']:.2f}")
    print(f"  win_rate_no_st: {new['win_rate_no_st']*100:.1f}%")
    print(f"  n_picks_no_st: {new['n_picks_no_st']}, n_weeks: {new['n_weeks']}")

    if args.old_model:
        print(f"\n>>> 跑旧模型 {args.old_model}")
        old = evaluate_model_l2(args.old_model, pool_codes, args.start, args.end)
        print(f"  cum_alpha_no_st: {old['cum_alpha_no_st']*100:+.2f}%")
        print(f"  pf_no_st: {old['pf_no_st']:.2f}")
        delta = new["cum_alpha_no_st"] - old["cum_alpha_no_st"]
        verdict = "✅ 新模型胜出, 应上线" if delta > 0 else "❌ 新模型未升, 保留旧"
        print(f"\nΔ排 ST 累计 α: {delta*100:+.2f}pp → {verdict}")


if __name__ == "__main__":
    main()
