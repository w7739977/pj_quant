# 修复 Prompt — 中性化按截面分组

> 分支：`feature/simulated-trading`
> 基于 commit `f1c8a62`（中性化已实施但缺截面分组）
> 工程量：~30 分钟代码 + ~1.5 小时 evolve 验证

---

## 背景

实测发现：当前 `prepare_training_data` 把所有截面的样本（5491 只 × 64 截面 ≈ 35 万行）一起做中性化，等于"跨时段 + 跨股票一锅煮 zscore"，违反 Qlib 标准做法。

后果：R² 只升到 0.0316（vs 业界中性化 0.07-0.10），中性化效果打了 5 折。

正确做法（Qlib `CSZScoreNorm` 标准）：每个交易日单独做截面标准化（mean/std 在该日所有股票内计算）。

实测验证：
```
银行股 000001 三个截面 mom_20d: -0.16 / +0.09 / -0.01
中性化后:                     0.33 / 1.00 / 0.67
（同一只股票被排序成"行业内排名"，时序差异被错误消除）
```

---

## 修复方案

### 1. `ml/ranker.py:prepare_training_data` 给每条样本标记截面日期

**改动位置**: L84-94 滚动截面循环

```python
# 修改前
for end_idx in range(60, len(df) - forward_days, 20):
    window = df.iloc[:end_idx + 1]
    fwd = df.iloc[end_idx:end_idx + forward_days + 1]
    if len(fwd) < forward_days + 1:
        continue

    forward_return = float(fwd.iloc[-1]["close"]) / float(fwd.iloc[0]["close"]) - 1.0
    factors = {"code": sym, "label": forward_return}
    ...

# 修改后：增加 end_date 字段
for end_idx in range(60, len(df) - forward_days, 20):
    window = df.iloc[:end_idx + 1]
    fwd = df.iloc[end_idx:end_idx + forward_days + 1]
    if len(fwd) < forward_days + 1:
        continue

    forward_return = float(fwd.iloc[-1]["close"]) / float(fwd.iloc[0]["close"]) - 1.0
    end_date = str(window.iloc[-1]["date"])[:10]  # ← 新增：截面日期
    factors = {"code": sym, "label": forward_return, "end_date": end_date}  # ← 加字段
    ...
```

### 2. `factors/calculator.py` 新增按截面分组的中性化函数

**位置**: 在 `neutralize_factors` 函数下方追加

```python
def neutralize_factors_per_section(df: pd.DataFrame, factor_cols: list,
                                    section_col: str = "end_date",
                                    industry_col: str = "industry") -> pd.DataFrame:
    """
    按截面分组做中性化（Qlib CSZScoreNorm 标准做法）

    每个 section_col 唯一值（即一个交易日）单独执行：
      winsorize → cross_sectional_zscore → industry_neutralize

    Parameters
    ----------
    df : 含 section_col 字段的训练样本
    factor_cols : 待中性化的因子列
    section_col : 截面分组列（默认 "end_date"）
    industry_col : 行业列（默认 "industry"）

    Returns
    -------
    DataFrame: 同 shape，但因子列已被按截面中性化
    """
    if section_col not in df.columns:
        # 退化为一次性中性化（向后兼容，但应避免使用）
        import logging
        logging.getLogger(__name__).warning(
            f"无 {section_col} 列，退化为全局中性化（不推荐）"
        )
        return neutralize_factors(df, factor_cols, industry_col)

    df = df.copy()
    sections = df[section_col].unique()

    # 按截面循环，每个截面独立做完整流程
    for section in sections:
        mask = df[section_col] == section
        sub = df.loc[mask].copy()
        # 在该截面内做 winsorize + zscore + industry_rank
        sub = winsorize_cross_section(sub, factor_cols)
        sub = cross_sectional_zscore(sub, factor_cols)
        sub = industry_neutralize(sub, factor_cols, industry_col)
        # 写回原 df
        df.loc[mask, factor_cols] = sub[factor_cols].values

    return df
```

### 3. `ml/ranker.py:prepare_training_data` 改用新函数

**改动位置**: L124-137

```python
# 修改前
from factors.calculator import neutralize_factors
...
train_df = neutralize_factors(train_df, factor_cols)

# 修改后
from factors.calculator import neutralize_factors_per_section
...
# 注意 factor_cols 不含 end_date / industry / code / label
factor_cols = [c for c in train_df.columns
               if c not in ("label", "code", "industry", "end_date")]
train_df = neutralize_factors_per_section(train_df, factor_cols,
                                           section_col="end_date",
                                           industry_col="industry")
logger.info(f"  因子中性化完成 (按 {train_df['end_date'].nunique()} 个截面分组)")
```

### 4. `ml/ranker.py:train_model` 训练前移除 end_date 列

`end_date` 是辅助字段不应进入特征矩阵。检查 L155 附近：

```python
def train_model(train_df: pd.DataFrame) -> dict:
    ...
    # 清理数据
    df = train_df.dropna(subset=["label"])
    X = df[FEATURE_COLS].copy()  # ← 已用 FEATURE_COLS 过滤，end_date 不在其中，无需改
    y = df["label"].copy()
    ...
```

如果 `FEATURE_COLS` 里没有 `end_date`，无需改。但要确认：

```bash
python3 -c "from ml.ranker import FEATURE_COLS; print('end_date' in FEATURE_COLS, 'industry' in FEATURE_COLS)"
# 预期: False False
```

### 5. `predict` 函数无需改

`predict` 只处理实时单一截面（当日全市场一次预测），原 `neutralize_factors` 已经是"单截面"语义，无需分组。

---

## 单元测试

`tests/test_neutralization.py` 追加：

```python
def test_per_section_neutralize_isolates_dates():
    """同一只股票不同截面应该独立中性化"""
    import pandas as pd
    from factors.calculator import neutralize_factors_per_section

    df = pd.DataFrame({
        "code": ["a", "b", "a", "b", "a", "b"],
        "industry": ["X", "X", "X", "X", "X", "X"],
        "end_date": ["2024-01-01", "2024-01-01",
                     "2024-02-01", "2024-02-01",
                     "2024-03-01", "2024-03-01"],
        "mom_20d": [10, 20, 100, 200, 1000, 2000],  # 时序漂移
    })
    out = neutralize_factors_per_section(df, ["mom_20d"])
    # 每个截面只有 2 只股票（同行业），中性化后应为 0.5/1.0
    for date in df["end_date"].unique():
        sub = out[out["end_date"] == date]["mom_20d"]
        assert sub.min() == 0.5
        assert sub.max() == 1.0
```

```python
def test_per_section_handles_missing_section_col():
    """无 end_date 列时退化为全局中性化（向后兼容）"""
    import pandas as pd
    from factors.calculator import neutralize_factors_per_section

    df = pd.DataFrame({
        "code": ["a", "b"],
        "industry": ["X", "X"],
        "mom_20d": [10, 20],
    })
    out = neutralize_factors_per_section(df, ["mom_20d"])
    # 不应报错
    assert "mom_20d" in out.columns
```

---

## 验收清单

```bash
# 1. 单元测试
pytest tests/test_neutralization.py -v
# 预期: 6 项全过（原 4 + 新 2）

# 2. 烟囱验证：模拟同一股票多截面，确认隔离
python3 -c "
import pandas as pd
from factors.calculator import neutralize_factors_per_section

df = pd.DataFrame({
    'code': ['000001', '000001', '000001'],
    'industry': ['银行', '银行', '银行'],
    'end_date': ['2024-01-01', '2024-02-01', '2024-03-01'],
    'mom_20d': [-0.16, 0.09, -0.01],  # 时序变化
})
print('修复前 vs 修复后:')
out = neutralize_factors_per_section(df, ['mom_20d'])
print(out)
# 预期：每行的 mom_20d 都是 1.0（每个截面只有它自己 → 行业内排名 = 1.0）
# 这是合理的，因为单截面只有 1 只股票时无法横向比较
"

# 3. 跑 evolve（约 1.5 小时）
python3 main.py evolve

# 4. 验收 R²
# 预期: cv_r2_mean ≥ 0.06（vs 当前 0.0316，提升至少 90%）
# 业界中性化标配能到 0.07-0.10
```

---

## 提交规范

单 commit:

```
fix(ranker): 中性化按截面分组（Qlib CSZScoreNorm 标准做法）

实测发现 prepare_training_data 把所有截面（5491 股票 × 64 截面 ≈ 35 万行）
一锅煮做 zscore，违反"截面标准化"语义。

后果: R² 只升到 0.0316（vs 业界 0.07-0.10），中性化效果打 5 折。

修复:
- prepare_training_data 给每条样本标记 end_date
- factors/calculator 新增 neutralize_factors_per_section
  按 end_date 分组，每个截面独立 winsorize + zscore + industry_rank
- ranker 改用 per_section 版本

向后兼容: 原 neutralize_factors 保留，predict 仍用单截面版本

预期效果: cv_r2_mean 0.0316 → 0.07-0.10

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 风险与回退

| 风险 | 缓解 |
|------|------|
| 截面太少（每天股票数太少）导致行业内只有 1-2 只 | `industry_neutralize` 内部有 `if valid.sum() < 5: continue` 兜底 |
| 早期截面（2020 年）某些行业仅几只股票 | 同上，自动跳过 |
| `end_date` 字段意外进入 FEATURE_COLS | FEATURE_COLS 是显式列表，不含 end_date，不会进入特征矩阵 |
| evolve 跑完 R² 仍 < 0.06 | 检查截面数（应 ≥ 60）+ 行业覆盖率（应 ≥ 95%）；最差情况下回退用旧模型 |

回退：单 commit，git revert 即可。

---

## 不在本次范围

- 情绪因子并发（仍是阶段 1 串行 45 分钟，下次月度 evolve 受益）
- ML 主导 final_score 的权重微调（0.7/0.3 留给数据验证后调）
- 8 维度推送验证（等模型上线后单独跑 live 验证）
