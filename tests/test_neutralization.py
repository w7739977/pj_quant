"""测试中性化预处理"""
import pandas as pd
import numpy as np
from factors.calculator import (
    winsorize_cross_section, cross_sectional_zscore,
    industry_neutralize, neutralize_factors,
    neutralize_factors_per_section,
)


def test_winsorize_clips_extremes():
    df = pd.DataFrame({"x": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 1000]})
    out = winsorize_cross_section(df, ["x"], lower=0.05, upper=0.95)
    assert out["x"].max() < 1000   # 极值被裁


def test_zscore_normalizes():
    df = pd.DataFrame({"x": list(range(1, 21))})  # 20 rows to pass min(10) threshold
    out = cross_sectional_zscore(df, ["x"])
    assert abs(out["x"].mean()) < 1e-6
    assert abs(out["x"].std(ddof=1) - 1) < 0.1


def test_industry_neutralize_within_group():
    df = pd.DataFrame({
        "code": ["a", "b", "c", "d"],
        "x": [10, 20, 100, 200],
        "industry": ["A", "A", "B", "B"],
    })
    out = industry_neutralize(df, ["x"])
    # 每个行业内最大值应该是 1.0
    assert out[out["industry"] == "A"]["x"].max() == 1.0
    assert out[out["industry"] == "B"]["x"].max() == 1.0


def test_neutralize_pipeline():
    """完整流程不崩"""
    df = pd.DataFrame({
        "code": [f"{i:06d}" for i in range(20)],
        "industry": (["A", "B"] * 10),
        "mom_20d": np.random.randn(20),
        "pe_ttm": np.random.uniform(5, 50, 20),
    })
    out = neutralize_factors(df, ["mom_20d", "pe_ttm"])
    # 中性化后每列应该在 0-1 之间（行业内排名）
    assert (out["mom_20d"].between(0, 1) | out["mom_20d"].isna()).all()


def test_per_section_neutralize_isolates_dates_zscore():
    """同一只股票不同截面应该独立 zscore（默认无行业排名）

    每截面 10 只股票（满足 cross_sectional_zscore 最小样本阈值）
    时序漂移应被截面隔离消除（每截面均值≈0）
    """
    rows = []
    for date_idx, date in enumerate(["2024-01-01", "2024-02-01", "2024-03-01"]):
        for i in range(10):
            rows.append({
                "code": f"s{i:02d}",
                "industry": "X",
                "end_date": date,
                # 时序漂移: 第 1 截面 ~10, 第 2 截面 ~100, 第 3 截面 ~1000
                "mom_20d": (10 ** (date_idx + 1)) + i * (10 ** date_idx),
            })
    df = pd.DataFrame(rows)
    out = neutralize_factors_per_section(df, ["mom_20d"])
    # 每截面均值应≈0（zscore 中心化）
    for date in df["end_date"].unique():
        sub = out[out["end_date"] == date]["mom_20d"]
        assert abs(sub.mean()) < 1e-6, f"截面 {date} mean={sub.mean()}"
        # std 接近 1
        assert abs(sub.std(ddof=1) - 1.0) < 0.1


def test_per_section_neutralize_with_industry():
    """显式开启 apply_industry=True 时使用行业内排名"""
    df = pd.DataFrame({
        "code": ["a", "b", "a", "b", "a", "b"],
        "industry": ["X", "X", "X", "X", "X", "X"],
        "end_date": ["2024-01-01", "2024-01-01",
                     "2024-02-01", "2024-02-01",
                     "2024-03-01", "2024-03-01"],
        "mom_20d": [10, 20, 100, 200, 1000, 2000],
    })
    out = neutralize_factors_per_section(df, ["mom_20d"], apply_industry=True)
    # 行业内排名：每截面 2 只 → 0.5 / 1.0
    for date in df["end_date"].unique():
        sub = out[out["end_date"] == date]["mom_20d"]
        assert sub.min() == 0.5
        assert sub.max() == 1.0


def test_per_section_handles_missing_section_col():
    """无 end_date 列时退化为全局中性化（向后兼容）"""
    df = pd.DataFrame({
        "code": ["a", "b"],
        "industry": ["X", "X"],
        "mom_20d": [10, 20],
    })
    out = neutralize_factors_per_section(df, ["mom_20d"])
    # 不应报错
    assert "mom_20d" in out.columns
