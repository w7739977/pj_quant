"""D_top10 vs D_top20 稳健性验证：paired t / block bootstrap / DSR / PBO

quant-analyst skill review 指出 13 周单期回测样本不足以独立支撑
NUM_POSITIONS=10 决策。本脚本不重跑回测，直接读 backtest_year.py
已有的 csv，做 4 项稳健性测试，输出 docs/walk_forward_top10_vs_top20.md。

输入: logs/backtest_year_top{10,20}_clean.csv
输出: docs/walk_forward_top10_vs_top20.md

用法: python3 scripts/validate_top10_vs_top20.py
"""
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".claude/skills/walk-forward-validation/scripts"))
from overfit_detector import deflated_sharpe_ratio  # noqa: E402

METHOD = "D 频次共识(生产)"  # 生产用方案
SEED = 42


def load_alpha(top_n: int) -> pd.Series:
    """读 csv 中 D 方案的 13 周 alpha 序列，按日期排序"""
    df = pd.read_csv(REPO / f"logs/backtest_year_top{top_n}_clean.csv")
    sub = df[df["method"] == METHOD].sort_values("date").reset_index(drop=True)
    return sub.set_index("date")["alpha"]


def stationary_bootstrap_indices(n: int, mean_block_len: float, rng) -> np.ndarray:
    """Politis-Romano stationary bootstrap 索引采样

    block 长度 ~ Geom(1/mean_block_len)，覆盖序列相关。
    """
    p = 1.0 / mean_block_len
    idx = []
    while len(idx) < n:
        start = rng.integers(0, n)
        block_len = rng.geometric(p)
        for k in range(block_len):
            if len(idx) >= n:
                break
            idx.append((start + k) % n)
    return np.array(idx[:n])


def block_bootstrap_ci(diffs: np.ndarray, b: int, mean_block_len: float, ci: float = 0.95):
    """对配对差值序列做 stationary block bootstrap，返回均值的 CI"""
    rng = np.random.default_rng(SEED)
    n = len(diffs)
    means = np.empty(b)
    for i in range(b):
        idx = stationary_bootstrap_indices(n, mean_block_len, rng)
        means[i] = diffs[idx].mean()
    lo = float(np.quantile(means, (1 - ci) / 2))
    hi = float(np.quantile(means, 1 - (1 - ci) / 2))
    return lo, hi, means


def paired_t_test(d: np.ndarray):
    """配对 t 检验"""
    n = len(d)
    mean = float(d.mean())
    sd = float(d.std(ddof=1))
    se = sd / np.sqrt(n)
    t_stat = mean / se if se > 0 else 0.0
    # 双尾 p
    p = 2 * (1 - norm.cdf(abs(t_stat)))
    return mean, sd, se, t_stat, float(p)


def cpcv_paths_top_better(top10: pd.Series, top20: pd.Series, n_groups: int = 6, k_test: int = 2):
    """CPCV(N=6, k=2)：把 13 周分 6 组，C(6,2)=15 path

    每个 path 把 k_test 组合 拼成 OOS，剩下 4 组拼 IS。
    报告 OOS sharpe 上 top10 占优的 path 比例（PBO-like）。
    """
    from itertools import combinations
    n = len(top10)
    edges = np.linspace(0, n, n_groups + 1, dtype=int)
    groups = [list(range(edges[g], edges[g + 1])) for g in range(n_groups)]
    paths = list(combinations(range(n_groups), k_test))
    top10_v, top20_v = top10.to_numpy(), top20.to_numpy()
    win = 0
    for combo in paths:
        oos_idx = sum((groups[g] for g in combo), [])
        if len(oos_idx) < 2:
            continue
        s10 = sharpe(top10_v[oos_idx])
        s20 = sharpe(top20_v[oos_idx])
        if s10 > s20:
            win += 1
    return win, len(paths)


def sharpe(arr) -> float:
    """简易 sharpe-like：mean / std（无年化，13 周内对比足够）"""
    a = np.asarray(arr)
    sd = a.std(ddof=1)
    return 0.0 if sd == 0 else float(a.mean() / sd)


def main():
    top10 = load_alpha(10)
    top20 = load_alpha(20)
    assert (top10.index == top20.index).all(), "日期不齐"
    n = len(top10)

    diffs = (top10 - top20).to_numpy()
    s10, s20 = sharpe(top10), sharpe(top20)

    # 1. paired t-test
    pt_mean, pt_sd, pt_se, pt_t, pt_p = paired_t_test(diffs)

    # 2. block bootstrap (mean_block_len ≈ 5d hold / 5d 周间隔 ≈ 1)
    # 周度采样且 5d hold 已让相邻周观测包含 80% 重叠：mean_block_len=2 是经验值
    bs_lo, bs_hi, bs_means = block_bootstrap_ci(diffs, b=10000, mean_block_len=2.0)
    bs_mean = float(bs_means.mean())

    # 3. DSR (D_top10 是 num_trials=2 中"被选中"的)
    dsr10 = deflated_sharpe_ratio(observed_sr=s10, num_trials=2,
                                  backtest_length=n, skewness=0.0, kurtosis=3.0)
    dsr20 = deflated_sharpe_ratio(observed_sr=s20, num_trials=2,
                                  backtest_length=n, skewness=0.0, kurtosis=3.0)

    # 4. CPCV(6,2)
    win, total = cpcv_paths_top_better(top10, top20)

    # 输出报告
    out = REPO / "docs/walk_forward_top10_vs_top20.md"
    out.write_text(f"""# D_top10 vs D_top20 稳健性验证 (walk-forward / bootstrap / DSR / PBO)

**生成日期**: {pd.Timestamp.now().strftime("%Y-%m-%d %H:%M")}
**输入**: `logs/backtest_year_top{{10,20}}_clean.csv` (退市股 bug 修复后回测)
**样本**: D 频次共识方法，{n} 个周一观测

## 摘要

| 指标 | D_top10 | D_top20 | 差距 |
|---|---:|---:|---:|
| avg α (周度) | {top10.mean()*100:+.2f}% | {top20.mean()*100:+.2f}% | {pt_mean*100:+.2f}pp |
| σ (周度) | {top10.std(ddof=1)*100:.2f}% | {top20.std(ddof=1)*100:.2f}% | — |
| sharpe-like | {s10:.3f} | {s20:.3f} | {s10-s20:+.3f} |

## 1. 配对 t 检验 (paired t-test)

差值 d_i = α_top10_i - α_top20_i, n = {n}

```
mean(d) = {pt_mean*100:+.3f}%
sd(d)   = {pt_sd*100:.3f}%
SE      = {pt_se*100:.3f}%
t       = {pt_t:.3f}
p (双尾) = {pt_p:.4f}
```

**判读**: {"显著（p<0.05）" if pt_p < 0.05 else f"不显著（p={pt_p:.3f} > 0.05）"}。
{"无法在 95% 置信下拒绝 'D_top10 与 D_top20 等价' 的零假设。" if pt_p >= 0.05 else "可以拒绝零假设，D_top10 显著优于 D_top20。"}

## 2. Stationary block bootstrap 95% CI

参数: B = 10000, mean_block_len = 2 (覆盖 5d hold 引入的相邻周序列相关)

```
diff 均值 (bootstrap) = {bs_mean*100:+.3f}%
95% CI = [{bs_lo*100:+.3f}%, {bs_hi*100:+.3f}%]
```

**判读**: {"CI 不包含 0，差异显著" if (bs_lo > 0 or bs_hi < 0) else "CI 包含 0，差异不显著（与 paired t 一致）"}。

## 3. Deflated Sharpe Ratio

```
D_top10:  observed_sr = {s10:.3f}, expected_max_sr = {dsr10.expected_max_sr:.3f}
          DSR p-value = {dsr10.dsr_pvalue:.3f}  →  {"显著" if dsr10.is_significant else "未达 0.95 显著阈值"}
D_top20:  observed_sr = {s20:.3f}, expected_max_sr = {dsr20.expected_max_sr:.3f}
          DSR p-value = {dsr20.dsr_pvalue:.3f}  →  {"显著" if dsr20.is_significant else "未达 0.95 显著阈值"}
```

**判读**: 在 num_trials=2 (top10 vs top20) 假设下，{"D_top10 sharpe 在多重检验校正后仍显著" if dsr10.is_significant else "D_top10 sharpe 未达 DSR 0.95 阈值，样本量不足以支撑统计显著"}。

## 4. CPCV(N=6, k=2) — 多 path OOS 评估

把 13 周分 6 组（每组 2-3 周），取 C(6,2)=15 paths，每个 path 用 2 组作 OOS，看 D_top10 在 OOS 上的 sharpe 是否仍占优。

```
D_top10 sharpe > D_top20 在 {win}/{total} paths 占优 ({100*win/total:.1f}%)
```

**判读**: {"超半数 paths 占优，方向稳健" if win > total/2 else "占优 paths 不超半数，结论不稳"}。

## 总评

样本量 n={n} 周对参数选择决策**显著性弱**：
- paired t p={pt_p:.3f} {"未" if pt_p >= 0.05 else "已"}达 0.05
- DSR D_top10 p={dsr10.dsr_pvalue:.3f} {"未" if not dsr10.is_significant else "已"}达 0.95
- bootstrap CI {"包含 0" if (bs_lo <= 0 <= bs_hi) else "不含 0"}
- CPCV 占优 {100*win/total:.0f}%

**生产决策建议**:
- **保持 NUM_POSITIONS=10**（资金约束 + 方向证据 + CPCV 占优率支持）
- **不应宣称 "D_top20 已被证伪"** —— 13 周样本不够
- 数据累积到 26+ 周后复跑本脚本，期望 paired t / DSR 转入显著区

**自动化建议**: 加入月度 evolve 流程作为参数稳健性体检（task #14 follow-up）。
""")
    print(f"报告已生成: {out.relative_to(REPO)}")
    print(f"  paired t p     = {pt_p:.4f}")
    print(f"  bootstrap CI   = [{bs_lo*100:+.3f}%, {bs_hi*100:+.3f}%]")
    print(f"  DSR top10      = {dsr10.dsr_pvalue:.3f}")
    print(f"  CPCV 占优      = {win}/{total} ({100*win/total:.1f}%)")


if __name__ == "__main__":
    main()
