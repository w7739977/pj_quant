# D_top10 vs D_top20 稳健性验证 (walk-forward / bootstrap / DSR / PBO)

**生成日期**: 2026-05-10 18:39
**输入**: `logs/backtest_year_top{10,20}_clean.csv` (退市股 bug 修复后回测)
**样本**: D 频次共识方法，13 个周一观测

> **⚠️ 信号视角，不是实盘 PnL**：本报告所有 α 基于「5d hold」假设（与回测训练标签对齐）。生产实盘是动态 hold（止损/止盈/超时三规则），不强制 5d 卖。下面统计检验的是「信号能否区分两个 NUM_POSITIONS 参数下的横截面 alpha」，不是「实盘 PnL 谁更高」。

## 摘要

| 指标 | D_top10 | D_top20 | 差距 |
|---|---:|---:|---:|
| avg α (周度) | +1.15% | +0.51% | +0.64pp |
| σ (周度) | 2.31% | 1.42% | — |
| sharpe-like | 0.498 | 0.359 | +0.139 |

## 1. 配对 t 检验 (paired t-test)

差值 d_i = α_top10_i - α_top20_i, n = 13

```
mean(d) = +0.643%
sd(d)   = 1.466%
SE      = 0.407%
t       = 1.582
p (双尾) = 0.1137
```

**判读**: 不显著（p=0.114 > 0.05）。
无法在 95% 置信下拒绝 'D_top10 与 D_top20 等价' 的零假设。

## 2. Stationary block bootstrap 95% CI

参数: B = 10000, mean_block_len = 2 (覆盖 5d hold 引入的相邻周序列相关)

```
diff 均值 (bootstrap) = +0.642%
95% CI = [+0.027%, +1.273%]
```

**判读**: CI 不包含 0，差异显著。

## 3. Deflated Sharpe Ratio

```
D_top10:  observed_sr = 0.498, expected_max_sr = 0.520
          DSR p-value = 0.472  →  未达 0.95 显著阈值
D_top20:  observed_sr = 0.359, expected_max_sr = 0.520
          DSR p-value = 0.295  →  未达 0.95 显著阈值
```

**判读**: 在 num_trials=2 (top10 vs top20) 假设下，D_top10 sharpe 未达 DSR 0.95 阈值，样本量不足以支撑统计显著。

## 4. CPCV(N=6, k=2) — 多 path OOS 评估

把 13 周分 6 组（每组 2-3 周），取 C(6,2)=15 paths，每个 path 用 2 组作 OOS，看 D_top10 在 OOS 上的 sharpe 是否仍占优。

```
D_top10 sharpe > D_top20 在 10/15 paths 占优 (66.7%)
```

**判读**: 超半数 paths 占优，方向稳健。

## 总评

样本量 n=13 周对参数选择决策**显著性弱**：
- paired t p=0.114 未达 0.05
- DSR D_top10 p=0.472 未达 0.95
- bootstrap CI 不含 0
- CPCV 占优 67%

**生产决策建议**:
- **保持 NUM_POSITIONS=10**（资金约束 + 方向证据 + CPCV 占优率支持）
- **不应宣称 "D_top20 已被证伪"** —— 13 周样本不够
- 数据累积到 26+ 周后复跑本脚本，期望 paired t / DSR 转入显著区

**自动化建议**: 加入月度 evolve 流程作为参数稳健性体检（task #14 follow-up）。
