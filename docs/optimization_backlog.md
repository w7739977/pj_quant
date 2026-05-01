# 优化待办（Optimization Backlog）

> 已识别但暂不实施的优化项，按优先级排序。每条记录场景、当前耗时、预期收益、改动范围。

---

## P0 — 全市场策略 R² 下降问题（2026-05-02 实测）

### 现象

取消中小盘限制（`max_cap` 5e9 → 1e13，含科创板/北交所）后跑 evolve：

| 池范围 | 股票数 | 训练样本 | cv_r2_mean |
|--------|--------|---------|-----------|
| 5-50 亿（旧） | 1437 | 9.2 万 | 0.0902 |
| 5-100 亿 | 1437 | 9.2 万 | 0.0869 |
| 5+亿全市场 | 5491 | 34.8 万 | **0.0251** ↓ -71% |

样本量翻 4 倍但 R² 反降 71%，说明全市场策略导致**模式混杂**：

- 大盘股（500+亿）波动小、动量信号弱、PE 稳定
- 科创板高估值 PE 数百是常态 → `pe_ttm` 因子含义偏移
- 北交所流动性低 → 量价因子噪声大
- 现有 XGBoost 单模型无法对不同板块用不同策略

### 现状决策

**保留旧模型（5-50 亿小盘 R²=0.0902）作为生产模型**。新模型保存为 candidate 不上线。

### 优化方向（按工程量递增）

#### 方向 1（最简单）— 回退到小盘池训练 + 全市场推荐

**改动**: `ml/auto_evolve.py:evolve` 显式传 `compute_stock_pool_factors(max_cap=5e9)`，但 `portfolio/allocator.py` 推荐时仍 `max_cap=1e13`。

**问题**: 训练-推理池不一致，已是 P1 旧问题。但对小盘股的预测质量好，对中大盘是"延伸应用"风险可控。

**预期 R²**: 回到 0.087-0.090

#### 方向 2 — 板块独立训练（多模型集成）

**改动**:
```python
# 训练时按板块拆分
small_cap_model = XGBRegressor()  # 5-50亿训
mid_cap_model = XGBRegressor()    # 50-500亿训
large_cap_model = XGBRegressor()  # >500亿训
star_model = XGBRegressor()       # 科创板单独训

# 推荐时按股票市值路由到对应模型
```

**工程量**: 改 `ml/ranker.py:train_model`、`predict`、`get_model_info` 各处适配多模型，约 2-3 天。

**预期 R²**: 各板块模型独立，预计平均 0.06-0.08，但每板块策略针对性强，实战胜率可能更高。

#### 方向 3 — 加板块/市值作为模型特征

**改动**:
```python
FEATURE_COLS = [..., "market_cap_log", "board_id"]  # log(市值) + 板块独热
```

让 XGBoost 自己学到"这是大盘 vs 小盘"的差异。最小改动，但需要每只股票补板块字段。

**工程量**: 1 天。

**预期 R²**: 0.05-0.07

#### 方向 4（业界主流）— 因子标准化按截面 + 板块中性化

每个截面对每个因子做 (x - mean) / std，按板块分组中性化（行业内排名）。这是九坤/华泰金工等专业团队的做法。

**工程量**: 改 `factors/calculator.py` 加 `cross_sectional_normalize()` 工具，约 3-5 天。

**预期 R²**: 0.07-0.10

### 推荐路径

短期：**方向 1**（10 分钟改动），保住小盘 R²=0.09 的有效模型，下次 evolve 仍跑全市场池，但只取小盘子集训。

中期：**方向 3**（1 天），让模型自己学板块差异。

长期：**方向 2 或 4**（2-5 天），需要更多数据科学投入。

### 与 docs/eight_dimensions_plan.md 的关系

`eight_dimensions_plan.md` P1 章节已建议"ML 端到端预测直接排序"（Qlib 方式），那是**合并**因子排名 + ML 排名公式的问题。本节是**取消池范围后**的新问题，独立的优化方向。两者不冲突。

---

## P0 — 立即可做但有风险

### 1. 情绪因子计算并发改造

**文件**: `factors/calculator.py:_batch_sentiment_factors`

**当前问题（2026-05-01 实测）**:
- 全市场池 5589 只股票，情绪计算分 2 阶段串行：
  - 阶段 1: `fetch_stock_news` 单只 HTTP × 5491 次 ≈ **45 分钟**
  - 阶段 2: `flash_tag_sentiment` 每批 20 只调 GLM × 275 批 ≈ **30 分钟**
- 总计约 **75 分钟**
- 都是 IO 密集（网络 + GLM token 生成），完美并行场景

**预期改造**:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

# 阶段 1: 10 并发抓新闻 (45min → 5min)
with ThreadPoolExecutor(max_workers=10) as ex:
    results = list(ex.map(fetch_stock_news, symbols))

# 阶段 2: 5 并发调 GLM (30min → 6min)
with ThreadPoolExecutor(max_workers=5) as ex:
    futures = {ex.submit(call_glm_batch, b): b for b in batches}
    for f in as_completed(futures):
        ...
```

**预期收益**: 75 min → **~12 min**（6x 加速）

**风险**:
- GLM API 限流（HTTP 429）— 实测当前已有 timeout 警告
- 东方财富新闻 API 封 IP（fetch_stock_news 同源高并发）
- ThreadPool 写错可能死锁

**建议落地步骤**:
1. 先小批量测试（200 只，比对串行 vs 并行结果一致性）
2. GLM 并发降级机制：429 自动 fallback 到串行 + sleep
3. fetch_stock_news 加 retry + jitter

**触发时机**: 下次月度 evolve 之前（6/1 cron 之前）。如果不改，每月 evolve 就要 ~75 分钟卡在情绪上。

---

## P1 — 中等优先

### 2. fund-flow 因子化（FIX_PROMPT_FUND_FLOW v2）

**文件**: `data/fund_flow_fetcher.py`（新建）+ `factors/calculator.py`

**背景**: e341282 已加资金流"展示"但没进入 ML 决策。`feature/fund-flow-factor` 分支有现成 prompt，重启时切过去做。

### 3. 训练-推理池一致性问题

**已解决**（2026-05-01）：训练和推荐池统一为 `min_cap=5e8, max_cap=1e13`，含科创板/北交所。

---

## P2 — 长期改进

### 4. 因子/ML 权重方法论改造（方案 A）

详见 `docs/eight_dimensions_plan.md` P0/P1 章节，建议落地"ML 预测直接排序"取代当前"因子排名 + ML 排名公式合并"。

### 5. 8 维度分析模块

详见 `docs/eight_dimensions_plan.md` 主体设计。
