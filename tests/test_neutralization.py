"""测试中性化预处理"""
import pandas as pd
import numpy as np
from factors.calculator import (
    winsorize_cross_section, cross_sectional_zscore,
    industry_neutralize, neutralize_factors,
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
