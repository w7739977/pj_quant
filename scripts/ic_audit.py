"""
通用 IC 审计工具 — 统一了 ic_new_factors.py + ic_financial_factors.py 的方法学

任意因子在小盘股池上跑 Spearman IC 验证 (5720 只 × N 截面)
业界阈值: |IC| > 0.02 且 |IR| > 0.3 算「有用」, 顶级因子 IR > 0.5

用法:
  # 内置因子组 (基于 ml.ranker.FEATURE_COLS 自动计算)
  python3 scripts/ic_audit.py --preset baseline   # 23 个生产因子
  python3 scripts/ic_audit.py --preset financial  # 4 个财务因子
  python3 scripts/ic_audit.py --preset chip       # P0 双子套 5 个筹码因子 (需 feature 分支)
  python3 scripts/ic_audit.py --preset ohlc       # P0 双子套 4 个 OHLC 因子 (需 feature 分支)
  python3 scripts/ic_audit.py --preset all        # 全部

  # 自定义截面
  python3 scripts/ic_audit.py --preset baseline --sections 2025-09-01,2025-12-01,2026-03-01

  # 自定义 forward days / 市值范围
  python3 scripts/ic_audit.py --preset baseline --forward 10 --min-cap 1e9 --max-cap 5e9
"""
from __future__ import annotations
import sys
import time
import bisect
import argparse
from typing import Callable

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

sys.path.insert(0, "/Users/wei/pj_quant")
from data.storage import load_stock_daily, list_cached_stocks  # noqa: E402

DEFAULT_SECTIONS = [
    "2025-09-01", "2025-10-08", "2025-11-03", "2025-12-01",
    "2026-01-05", "2026-02-09", "2026-03-03", "2026-04-01",
]


def _build_baseline_extractor() -> tuple[list, Callable]:
    """23 个生产 FEATURE_COLS 因子的截面提取器"""
    from factors.calculator import (
        calc_momentum, calc_volatility, calc_turnover_factor,
        calc_volume_price, calc_technical,
    )
    from data.financial_indicator import load_all_pit_to_dict
    fin_cache = load_all_pit_to_dict()
    print(f"  financial cache loaded: {len(fin_cache)} 只")

    def _lookup_fin(code: str, yyyymmdd: str) -> dict:
        history = fin_cache.get(code, [])
        if not history:
            return {}
        ann_dates = [h[0] for h in history]
        idx = bisect.bisect_right(ann_dates, yyyymmdd) - 1
        return history[idx][1] if idx >= 0 else {}

    from ml.ranker import FEATURE_COLS as cols

    def extract(window: pd.DataFrame, code: str, end_date: str) -> dict:
        f = {}
        f.update(calc_momentum(window))
        f.update(calc_volatility(window))
        f.update(calc_turnover_factor(window))
        f.update(calc_volume_price(window))
        f.update(calc_technical(window))
        last = window.iloc[-1]
        for c in ["pe_ttm", "pb", "turnover_rate", "volume_ratio"]:
            f[c] = last.get(c, np.nan)
        fin = _lookup_fin(code, end_date.replace("-", ""))
        for c in ["roe_yearly", "or_yoy", "dt_eps_yoy", "debt_to_assets"]:
            f[c] = fin.get(c, np.nan)
        return f

    return list(cols), extract


def _build_financial_extractor() -> tuple[list, Callable]:
    """只 4 个财务因子的截面提取器"""
    from data.financial_indicator import load_all_pit_to_dict
    fin_cache = load_all_pit_to_dict()
    print(f"  financial cache loaded: {len(fin_cache)} 只")

    def _lookup_fin(code: str, yyyymmdd: str) -> dict:
        history = fin_cache.get(code, [])
        if not history:
            return {}
        ann_dates = [h[0] for h in history]
        idx = bisect.bisect_right(ann_dates, yyyymmdd) - 1
        return history[idx][1] if idx >= 0 else {}

    cols = ["roe_yearly", "or_yoy", "dt_eps_yoy", "debt_to_assets"]

    def extract(window: pd.DataFrame, code: str, end_date: str) -> dict:
        fin = _lookup_fin(code, end_date.replace("-", ""))
        return {c: fin.get(c, np.nan) for c in cols}

    return cols, extract


def _build_chip_extractor() -> tuple[list, Callable]:
    """P0 双子套筹码 5 因子 (要求 factors.chip_factors 在 main, 否则 ImportError)"""
    from factors.chip_factors import calc_chip_factors  # type: ignore
    cols = ["CYQK_C", "ASR", "CKDW", "PRP", "CGO"]

    def extract(window: pd.DataFrame, code: str, end_date: str) -> dict:
        return calc_chip_factors(window)

    return cols, extract


def _build_ohlc_extractor() -> tuple[list, Callable]:
    """P0 双子套 OHLC 4 因子"""
    from factors.ohlc_factors import calc_ohlc_factors  # type: ignore
    cols = ["upper_shadow_20d", "lower_shadow_20d", "amplitude_20d", "amplitude_std_20d"]

    def extract(window: pd.DataFrame, code: str, end_date: str) -> dict:
        return calc_ohlc_factors(window)

    return cols, extract


PRESETS = {
    "baseline": _build_baseline_extractor,
    "financial": _build_financial_extractor,
    "chip": _build_chip_extractor,
    "ohlc": _build_ohlc_extractor,
}


def run_audit(factor_cols: list, extract: Callable, sections: list,
              min_cap: float, max_cap: float, forward_days: int,
              window_len: int = 80) -> None:
    all_syms = list_cached_stocks()
    print(f"cached: {len(all_syms)} 只, 截面 {len(sections)} 个")

    section_rows = {sec: [] for sec in sections}
    t0 = time.time()
    for i, sym in enumerate(all_syms):
        try:
            df = load_stock_daily(sym)
            if df is None or df.empty or len(df) < window_len + 1:
                continue
            df = df.copy()
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
            df = df.sort_values("date").reset_index(drop=True)
            for sec in sections:
                prior = df[df["date"] <= sec]
                if len(prior) < window_len + 1:
                    continue
                end_pos = prior.index[-1]
                mv_ser = prior["total_mv"].dropna()
                if mv_ser.empty:
                    continue
                cap = float(mv_ser.iloc[-1]) * 1e4
                if not (min_cap <= cap <= max_cap):
                    continue
                if end_pos + forward_days >= len(df):
                    continue
                f0 = float(df.iloc[end_pos + 1]["close"])
                fN = float(df.iloc[end_pos + forward_days]["close"])
                if f0 <= 0:
                    continue
                fwd_ret = fN / f0 - 1.0
                window = df.iloc[max(0, end_pos - window_len): end_pos + 1]
                factors = extract(window, sym, sec)
                r = {"code": sym, "fwd_ret": fwd_ret}
                r.update(factors)
                section_rows[sec].append(r)
        except Exception:
            pass
        if (i + 1) % 1000 == 0:
            print(f"  {i+1}/{len(all_syms)} ({time.time()-t0:.0f}s)")

    print(f"\n数据准备完成 ({time.time()-t0:.0f}s)\n")
    print("截面样本:")
    for sec in sections:
        print(f"  {sec}: {len(section_rows[sec])} 只")

    all_ic = {f: [] for f in factor_cols}
    print("\n按截面 IC:")
    short_cols = [c[:16] for c in factor_cols]
    print(f"{'截面':<12} " + " ".join(f"{c:>16}" for c in short_cols))
    for sec in sections:
        df = pd.DataFrame(section_rows[sec])
        if len(df) < 50:
            continue
        ics = []
        for f in factor_cols:
            if f not in df.columns:
                ics.append("missing")
                all_ic[f].append(np.nan)
                continue
            sub = df[[f, "fwd_ret"]].dropna()
            if len(sub) < 30:
                ics.append("n/a")
                all_ic[f].append(np.nan)
                continue
            ic, _ = spearmanr(sub[f], sub["fwd_ret"])
            all_ic[f].append(float(ic))
            ics.append(f"{ic:+.4f}")
        print(f"{sec:<12} " + " ".join(f"{x:>16}" for x in ics))

    print("\n" + "=" * 80)
    print(f"{'因子':<22} {'IC均值':>10} {'IC标准差':>10} {'IR':>8} {'同向率':>8} {'判定':>8}")
    print("=" * 80)
    pass_list = []
    fail_list = []
    for f in factor_cols:
        arr = np.array([x for x in all_ic[f] if not np.isnan(x)])
        if len(arr) < 3:
            print(f"{f:<22} (insufficient)")
            continue
        m = arr.mean()
        s = arr.std(ddof=1)
        ir = m / s if s > 1e-8 else np.nan
        pos = (arr > 0).mean()
        same_dir = pos if pos > 0.5 else 1 - pos
        ok = abs(m) > 0.02 and abs(ir) > 0.3
        tier = "★top" if abs(ir) > 0.5 else ("✓ pass" if ok else "✗ fail")
        if ok:
            pass_list.append((f, m, ir))
        else:
            fail_list.append((f, m, ir))
        print(f"{f:<22} {m:>+10.4f} {s:>10.4f} {ir:>+8.2f} {same_dir:>7.0%} {tier:>8}")
    print("=" * 80)
    print(f"业界阈值: |IC|>0.02 且 |IR|>0.3 (顶级 IR>0.5); 同向率 = 截面方向一致比例 (max 100%)")
    print(f"\n保留 {len(pass_list)} / 剔除 {len(fail_list)}")
    if fail_list:
        print(f"建议剔除: {[f for f, _, _ in fail_list]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", required=True, choices=list(PRESETS.keys()) + ["all"])
    ap.add_argument("--sections", default=",".join(DEFAULT_SECTIONS),
                    help="逗号分隔截面日期")
    ap.add_argument("--forward", type=int, default=5, help="forward days (默认 5d)")
    ap.add_argument("--min-cap", type=float, default=5e8)
    ap.add_argument("--max-cap", type=float, default=5e9)
    ap.add_argument("--window", type=int, default=80, help="因子计算回溯窗口")
    args = ap.parse_args()

    presets = list(PRESETS.keys()) if args.preset == "all" else [args.preset]
    sections = [s.strip() for s in args.sections.split(",") if s.strip()]

    for p in presets:
        print(f"\n{'='*80}\n>>> Preset: {p}\n{'='*80}")
        factor_cols, extract = PRESETS[p]()
        run_audit(factor_cols, extract, sections,
                  args.min_cap, args.max_cap, args.forward, args.window)


if __name__ == "__main__":
    main()
