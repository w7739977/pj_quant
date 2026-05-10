"""scripts.validate_top10_vs_top20 核心 helper 单测"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from validate_top10_vs_top20 import (  # noqa: E402
    paired_t_test, sharpe, stationary_bootstrap_indices,
    block_bootstrap_ci, cpcv_paths_top_better,
)


def test_sharpe_zero_std():
    assert sharpe([1.0, 1.0, 1.0]) == 0.0


def test_sharpe_positive():
    s = sharpe([1.0, 2.0, 3.0])
    assert s > 0


def test_sharpe_negative():
    assert sharpe([-1.0, -2.0, -3.0]) < 0


def test_paired_t_test_zero_diff():
    """全 0 差值 → t=0, p=1"""
    mean, sd, se, t, p = paired_t_test(np.zeros(10))
    assert mean == 0
    assert t == 0
    assert p == pytest.approx(1.0, abs=1e-9)


def test_paired_t_test_known():
    """已知 case：[1,1,1,1,1] mean=1, sd=0 → SE=0 → t=0 (避免除零)"""
    _, _, _, t, _ = paired_t_test(np.ones(5))
    assert t == 0  # se=0 → 设 t=0


def test_paired_t_test_distinct_values():
    """非常数序列：mean=1.0, sd>0, t>0"""
    mean, sd, se, t, p = paired_t_test(np.array([0.5, 1.0, 1.5, 1.0, 1.0]))
    assert mean == pytest.approx(1.0)
    assert sd > 0
    assert t > 0
    assert 0 < p < 1


def test_stationary_bootstrap_length():
    rng = np.random.default_rng(42)
    idx = stationary_bootstrap_indices(n=20, mean_block_len=3.0, rng=rng)
    assert len(idx) == 20
    assert all(0 <= i < 20 for i in idx)


def test_stationary_bootstrap_blocks_wrap():
    """blocks should wrap modulo n"""
    rng = np.random.default_rng(42)
    idx = stationary_bootstrap_indices(n=10, mean_block_len=20.0, rng=rng)
    assert len(idx) == 10
    # 长 block 时应当出现 wraparound (idx[k+1] == (idx[k]+1) % 10) 至少一次
    consecutive = sum(
        1 for k in range(len(idx) - 1) if idx[k + 1] == (idx[k] + 1) % 10
    )
    assert consecutive > 0


def test_block_bootstrap_ci_zero_diff():
    """全 0 差值 → CI 应当紧贴 0"""
    diffs = np.zeros(20)
    lo, hi, _ = block_bootstrap_ci(diffs, b=500, mean_block_len=2.0)
    assert lo == 0.0
    assert hi == 0.0


def test_block_bootstrap_ci_strong_positive():
    """全 +1 差值 → CI 应在 1 附近且不含 0"""
    diffs = np.ones(20)
    lo, hi, _ = block_bootstrap_ci(diffs, b=500, mean_block_len=2.0)
    assert lo == 1.0  # 全 1 差值，所有 bootstrap mean 都是 1
    assert hi == 1.0


def test_cpcv_paths_count():
    """N=6, k=2 → C(6,2)=15 paths；同噪声 / 不同 mean → s10 sharpe 始终更高"""
    import pandas as pd
    n = 13
    rng = np.random.default_rng(42)
    noise = rng.standard_normal(n) * 0.005
    s10 = pd.Series(0.03 + noise, index=range(n))   # mean=3%
    s20 = pd.Series(0.01 + noise, index=range(n))   # mean=1%, 同噪声
    win, total = cpcv_paths_top_better(s10, s20, n_groups=6, k_test=2)
    assert total == 15
    assert win == 15  # mean 高 std 同 → sharpe 高，所有 path 占优


def test_cpcv_top20_never_wins():
    """同噪声但 s10 mean 更低 → 0/15"""
    import pandas as pd
    n = 13
    rng = np.random.default_rng(42)
    noise = rng.standard_normal(n) * 0.005
    s10 = pd.Series(-0.02 + noise, index=range(n))  # mean=-2%
    s20 = pd.Series(0.02 + noise, index=range(n))   # mean=+2%, 同噪声
    win, total = cpcv_paths_top_better(s10, s20, n_groups=6, k_test=2)
    assert total == 15
    assert win == 0
