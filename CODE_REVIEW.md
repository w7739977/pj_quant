# 代码审查与优化报告

**审查日期**: 2026-04-07
**审查范围**: 数据获取、入库验证、模型训练

---

## 一、数据获取模块

### 1.1 发现的问题

| 文件 | 问题 | 严重性 |
|------|------|--------|
| `factors/data_loader.py` | 数据源优先级未包含 Tushare | 中 |
| `data/bulk_fetcher.py` | 仅支持 BaoStock K线获取 | 中 |
| `data/tushare_fundamentals.py` | 入库后无验证 | 中 |

### 1.2 优化方案

**新增文件: `data/tushare_loader.py`**
- 统一 Tushare K线 + 基本面获取
- 按日期批量 (0.3s/日期, 全市场5000+只)
- Parquet 缓存 → SQLite 批量导入
- 内置验证函数 `verify_import()`

**优势对比:**
| 指标 | BaoStock | Tushare |
|------|----------|---------|
| K线获取 | 3-5s/只 | 0.3s/全市场 |
| 基本面 | 3-5s/只 | 0.3s/全市场 |
| 4417只耗时 | ~5小时 | ~25分钟 |
| 并发支持 | 不支持 | 支持(按日期) |

---

## 二、数据入库验证 (本地优先架构)

### 2.1 发现的问题

```python
# tushare_fundamentals.py 原代码 line 197-199
conn.close()
elapsed = time.time() - t0
print(f"\n入库完成: ...")
return len(updated_stocks)
```
**问题**: 入库后无验证，无法发现:
- SQLite 写入失败
- 数据类型转换错误
- 日期匹配错误

### 2.2 修复方案 (已集成)

**新增函数 `_verify_import()`**:
```
流程: Parquet源数据 → SQLite入库数据 → 抽样对比
      ↓              ↓                ↓
   读取3只股票   读取对应3只    数值比对(误差<0.01)
```

**验证逻辑**:
1. 从 Parquet 读取某日期的源数据
2. 从 SQLite 读取同日期同股票的数据
3. 对比 pe_ttm, pb, ps_ttm, turnover_rate, volume_ratio
4. 输出 ✓/✗ 状态

**使用时机**: 每次 `import_to_sqlite()` 后自动执行

### 2.3 本地优先优势

由于使用 Parquet 作为本地缓存:
- 验证无需重新请求网络
- Parquet 文件可作为"真实来源"基准
- SQLite 数据异常可快速发现并重导

---

## 三、模型训练逻辑 (本地数据架构)

### 3.1 关键 Bug ⚠️

**文件**: `ml/ranker.py` line 101-103 (修复前)

```python
# 问题代码
# 基本面因子用 NaN 占位（非截面相关）
for col in ["pe_ttm", "pb", "turnover_rate", "volume_ratio", "sentiment_score"]:
    factors[col] = np.nan
```

**根本原因**:
- 代码注释"非截面相关"是误解
- 实际上本地 SQLite **已有**完整估值数据
- `window.iloc[-1]` 就是当日的基本面值

**影响**:
- 4个基本面因子完全无效
- 模型只用16个技术因子，浪费pe_ttm/pb等强信号
- Tushare 补全的数据 (22分钟) 未被利用

### 3.2 修复方案 (已集成)

**修复后的逻辑**:
```python
# 从本地 SQLite 读取当日基本面值
last_row = window.iloc[-1]  # window 的最后一行 = 截面当日
factors["pe_ttm"] = last_row.get("pe_ttm", np.nan)
factors["pb"] = last_row.get("pb", np.nan)
factors["turnover_rate"] = last_row.get("turnover_rate", np.nan)
factors["volume_ratio"] = last_row.get("volume_ratio", np.nan)
```

**数据流向**:
```
本地 SQLite (stock_XXXXXX)
  ↓
load_stock_daily(sym) 读取历史数据
  ↓
window = df[:end_idx+1]  滚动窗口
  ↓
window.iloc[-1]  当日行 → 提取 pe_ttm, pb, turnover_rate, volume_ratio
  ↓
factors 字典 → 训练样本
```

### 3.3 验证修复效果

训练后检查特征重要性:
```python
# 期望输出
{
    "pe_ttm": 0.1234,     # 应该 > 0
    "pb": 0.0987,         # 应该 > 0
    "turnover_rate": 0.05, # 应该 > 0
    "mom_20d": 0.15,      # 技术因子
    ...
}
```

如果基本面因子重要性 ≈ 0，说明修复未生效。

### 3.3 模型配置对比

| 配置项 | 当前值 | 业界建议 | 状态 |
|--------|--------|----------|------|
| 模型类型 | XGBRegressor | XGBRegressor ✓ | OK |
| n_estimators | 200 | 500 (with ES) | 可优化 |
| max_depth | 4 | 4-6 | OK |
| learning_rate | 0.05 | 0.01-0.05 | OK |
| reg_alpha | 无 | 0.1 | 缺失 |
| reg_lambda | 无 | 1.0 | 缺失 |
| early_stopping | 无 | 20轮 | 缺失 |
| CV方式 | K-Fold | 时间序列CV | 需改进 |

**K-Fold vs 时间序列CV:**
- K-Fold: 可能用未来数据预测过去 (数据泄露)
- 时间序列CV: train[t0:t1], val[t1:t2], test[t2:t3] — 更严谨

---

## 四、优化后文件清单

| 新文件 | 用途 |
|--------|------|
| `data/tushare_loader.py` | 统一 Tushare 数据获取 (K线+基本面) |
| `ml/ranker_optimized.py` | 优化的训练模块 (时间序列CV+正则化) |

| 修改文件 | 变更 |
|----------|------|
| `data/tushare_fundamentals.py` | 新增 `_verify_import()` 验证函数 |
| `ml/ranker.py` | 修复基本面因子 NaN bug |

---

## 五、建议执行顺序

1. **立即修复**: `ml/ranker.py` 基本面因子 bug
2. **测试验证**: 运行 `tushare_fundamentals.run()` 检查验证输出
3. **可选升级**: 使用 `ranker_optimized.py` 替换训练逻辑
4. **数据迁移**: 逐步用 `tushare_loader` 替换 BaoStock

---

## 六、验证清单

- [ ] 基本面因子在训练中有实际数据 (非 NaN)
- [ ] 入库后 `verify_import()` 抽样对比通过
- [ ] 特征重要性中 pe_ttm, pb 权重 > 0
- [ ] 交叉验证 R² 合理 (> 0.02)
- [ ] 时间序列 CV 分数与 K-Fold 无显著差异
