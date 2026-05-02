# A股量化系统 - 开发进度

## 2026-05-02 P0 财务因子 + 实验性方案的实证结论

### 背景

为了让模型学到更丰富的信号，本日做了三轮工程迭代。前两轮（全市场池 / 中性化）实证失败，第三轮（P0 财务因子）成功落地。本节客观记录方法论、实验数据和决策依据，避免后人重蹈覆辙。

### 关键实证结论（重要）

#### 1. 业界"中性化"在 A 股小盘策略下失效

**业界共识**：Qlib `CSZScoreNorm` + 行业中性化是全市场量化标配，能让 R² 从 0.05 → 0.07-0.10。

**实测结果**：

| 实验 | 池范围 | 中性化方法 | cv_r2_mean |
|------|-------|-----------|-----------|
| baseline | 5-50 亿小盘 | 无 | 0.0757（云主机生产）|
| v3 | 全市场 5491 | 全局 zscore (一锅煮) | 0.0316 |
| v5 | 全市场 5491 | 按截面 + 行业排名 | -0.0008 |
| v6 | 全市场 5491 | 按截面 + 仅 zscore | 0.0022 |
| v7 | 5-50 亿 | 按截面 + 仅 zscore | 0.0180 |
| **v8** | **5-50 亿** | **禁用** | **0.0650** |

**根因分析**：

- 全市场池（5491 只）和小盘池（1998 只）每个截面平均只有 ~84-240 只股票
- 业界中性化要求每个交易日 ~5500 只股票同步标准化（Qlib 训练数据是这样组织的）
- pj_quant 滚动截面方法（每股每 20 日生成一个样本）**截面密度太低**，导致：
  - 行业内常常 < 10 只样本，`rank(pct=True)` 退化为 0.5/1.0 等粗粒度值
  - `cross_sectional_zscore` 内部 `< 10` 阈值跳过，部分截面不标准化导致量级混乱
- 因子绝对量级信息（如 PE=11 vs PE=80）被破坏，模型反而学不到信号

**决策**：中性化代码保留（`neutralize_factors_per_section`），但**默认禁用**（`ENABLE_NEUTRALIZE=1` 才启用）。待因子库扩展到 50+ 或采用真正"全市场截面"训练数据时再评估开启。

#### 2. 全市场池策略对当前因子库不可行

实验 v3-v6 证明：取消市值限制（5e8~1e13）后 R² 从 0.0757 崩到 0.0022-0.0316。

**根因**：

- 大盘股（>500 亿）波动小、动量信号弱、PE 稳定 → 与小盘股逻辑相反
- 科创板 PE 数百是常态 → `pe_ttm` 因子方向偏移
- 北交所流动性低 → 量价因子噪声大
- XGBoost 单模型无法对不同板块用不同策略
- 业界中金 220 因子 + 多模型集成才能撑住全市场策略，pj_quant 22 因子不够

**决策**：回滚到 5-50 亿小盘池（`max_cap=5e9`）。is_tradeable 黑名单仍允许科创板/北交所代码（用户 50 万本金达开户门槛），但训练-推荐池一致都用小盘。

#### 3. 情绪因子在 ML 训练中贡献为零

**实测**：feature_importance['sentiment_score'] = 0.0000

**根因**：训练历史样本无法实时获取新闻 → `sentiment_score` 全 NaN → 中位数填充 → 同一值无信息量。

**决策**：

- evolve 时跳过情绪因子计算（`skip_sentiment=True`），节省 ~75 分钟训练时间
- 推送层仍用 FinBERT 实时打分（仅 top 10 推荐，1 秒内完成）
- 长期方案：sentiment_history 数据库（C 阶段）已实现代码，但 Tushare news 接口仅 2 次/天频限，无法批量回填，等待数据底座方案后启用

#### 4. FinBERT-Chinese 对中文负面新闻识别能力有限

**实测**（`yiyanghkust/finbert-tone-chinese`）：

```
"业绩超预期" → Positive 0.99 ✓
"暴雷退市"   → Neutral 0.99 ✗（应为 Negative）
"监管处罚"   → Neutral 0.90 ✗
```

**根因**：模型在 8k 研报上微调，研报多为正面/中性，负面样本严重不足。

**决策**：FinBERT 推理代码保留作为 GLM 限流时的兜底，但单元测试 `test_negative_news` 改为 xfail。长期换更平衡的中文金融情感模型（如 IDEA-CCNL/Erlangshen-RoBERTa）。

### 当前生产架构（v8 + P0 + 8 维度）

```
股票池: 5-50 亿小盘（max_cap=5e9）, ~2000 只
       └── is_tradeable 黑名单: 仅过滤 B 股（沪 B 900xxx / 深 B 200xxx）

因子库: 22 + 4 = 26 个
  动量(4): mom_5d, mom_10d, mom_20d, mom_60d
  波动率(2): vol_10d, vol_20d
  换手率(3): avg_turnover_5d, avg_turnover_20d, turnover_accel
  量价(2): vol_price_diverge, volume_surge
  技术(4): ma5_bias, ma10_bias, ma20_bias, rsi_14
  估值(2): pe_ttm, pb
  活跃(2): turnover_rate, volume_ratio
  情绪(1): sentiment_score（占位，训练时 NaN）
  财务(4): roe_yearly, or_yoy, dt_eps_yoy, debt_to_assets ← P0 新增

中性化: 默认禁用（实测在小盘策略下降低 R²）
       代码保留 ENABLE_NEUTRALIZE=1 启用，备未来全市场策略

ML 模型: XGBoost 回归预测 20 日收益
final_score = 0.7 × zscore(ML预测) + 0.3 × zscore(多因子综合)
            （ML 主导，因子作 sanity check）

推荐数: 10 只
8 维度展示: 盘面/大盘/行业/利好/量价/资金/业绩/订单
推送: 含目标价/止损价/风险收益比/AI 综合研判
```

### P0 财务因子实施（commit 6811d04）

#### 改动文件

| 文件 | 改动 |
|------|------|
| `data/financial_indicator.py` | 新建。Tushare `fina_indicator` 接入 + SQLite 表 + PIT 查询接口 |
| `main.py` | 加 `fetch-financial` 命令 |
| `factors/calculator.py` | `compute_all_factors` 集成 PIT 查询 |
| `ml/ranker.py` | FEATURE_COLS 加 4 列 + `_lookup_financial_pit` 二分查找 + `_FIN_CACHE` 全局缓存 |
| `strategy/small_cap.py` | `factor_direction` 加 4 项 + 1.5x 权重（财务核心） |
| `portfolio/allocator.py` | `reason_data.key_factors` 携带 4 因子 |
| `portfolio/reason_text.py` | 推送翻译（高 ROE / 营收高增 / 高负债 等）|
| `scripts/financial_monthly.py` | 每月增量 cron |
| `tests/test_financial_indicator.py` | 单元测试（save/load/PIT 隔离/缓存）|

#### PIT 数据正确性

训练时按 `ann_date`（公告日）过滤，避免未来数据泄露：

```
2024-04-30: 2024 一季报公告日
2024-05-15 截面: 可用 2024 一季报数据
2024-04-29 截面: 不可用（公告未发布）
```

`_lookup_financial_pit` 用 `bisect.bisect_right` 在每只股票按 ann_date 排序的列表中找最近一次 ≤ as_of_date 的公告，O(log n)。

#### 待验收

- 数据回填：`python3 main.py fetch-financial`（30 分钟）
- evolve 验证：cv_r2_mean 应从 v8 的 0.065 → 0.085-0.105
- 8 维度推送：reason 应展示"高 ROE 12%、营收增 25%"等

### 文档归档

11 个 `FIX_PROMPT*.md` 历史文档迁移到 `docs/archive/`，根目录仅保留：
- README.md / PROGRESS.md / DEPLOY.md

详见 `docs/archive/README.md`。

---

## 2026-04-30 模拟盘50万仓位 + 加权分配 + 推荐理由维度拆解

### 背景

模拟盘和实盘推荐初始资金从2万调整为50万，同步优化选股分配策略和推荐理由展示。

### 改动汇总

**1. 仓位调整（2万→50万）**

- `config/settings.py` — `SIM_INITIAL_CAPITAL` 20000→500000
- `simulation/engine.py` — 引擎初始化改用 `SIM_INITIAL_CAPITAL`（之前误用了回测的 `INITIAL_CAPITAL`）
- `simulation/trade_log.py` — 默认仓位同步改用 `SIM_INITIAL_CAPITAL`
- `sim_portfolio.json` / `sim_trading.db` — 重置为50万空仓，清空历史数据

**2. 等权分配→按 final_score 加权分配（收益最大化）**

`portfolio/allocator.py` `get_stock_picks_live` 资金分配逻辑重构：
- 旧：`per_stock = stock_capital / top_n`（等权）
- 新：按 `final_score`（=1/因子排名×100 + 1/ML排名×50 + 交集加分×20）归一化为权重，得分越高分配越多
- reason 中新增 `仓位XX%` 标注每只个股的资金占比

**3. 推荐理由维度拆解（分行展示）**

`portfolio/reason_text.py` `humanize_reason` 重构为分层格式：

```
锦泓集团：因子#1、ML#872、得分142.8
  技术面75分(优)｜20日涨+17.2%(强势)，RSI=65，波动4.4%，MA5偏离-3.3%
  基本面85分(优)｜PE=11(低估值)，PB=0.8(破净)，换手率7.5%(活跃)
  资金面65分(良)｜量比2.2(放量)，5日均换手6.8%，换手加速+15%
  ML预测｜偏多，预测20日+0.2%
  资金面｜主力净流入2846万，资金积极做多
```

- `portfolio/allocator.py` — reason_data 新增 `final_score`、`dimension_scores`、`dimension_details`（每个维度的具体因子值）
- `portfolio/reason_text.py` — `_format_dim_detail` 按维度翻译指标值+定性标签
- `simulation/report.py` — 维度得分展示兼容新数据结构

**4. Bug 修复**

- `simulation/engine.py` — numpy float32 JSON 序列化崩溃（新增 `_json_default` 处理器）
- allocator picks 中 numpy 类型统一 `_native()` 转换为 Python 原生类型

### 涉及文件

| 文件 | 改动 |
|------|------|
| `config/settings.py` | SIM_INITIAL_CAPITAL 50万 |
| `portfolio/allocator.py` | 加权分配 + dimension_details + numpy 类型安全 |
| `portfolio/reason_text.py` | 分行格式 + 维度指标拆解 + ML 预测分级 |
| `simulation/engine.py` | 用 SIM_INITIAL_CAPITAL + _json_default |
| `simulation/trade_log.py` | 默认仓位用 SIM_INITIAL_CAPITAL |
| `simulation/report.py` | dimension_scores 兼容 |

### 部署操作

- 模拟盘：`sim_portfolio.json` 重置为50万空仓，`sim_trading.db` 清空历史数据
- 实盘推荐：`quant.db` portfolio 表更新为50万空仓
- 旧模拟盘引擎 kill 后重新启动（新格式+50万仓位）
- cherry-pick 到 main 分支（`009c9ec`），解决6个文件冲突后推送


---

## 2026-04-30 之前的历史

详见 [`docs/archive/PROGRESS_2026Q1.md`](docs/archive/PROGRESS_2026Q1.md)（约 850 行，涵盖 2025-12 到 2026-04-27 的迭代）

主要里程碑：
- **2026-04-27** humanize_reason 结构化重构 + Py3.9 兼容
- **2026-04-27** 主力资金流向 + 推送格式优化
- **2026-04-25** 8 维度选股分析 + AI 综合研判
- **2026-04-20** 中性化按截面分组（CSZScoreNorm）
- **2026-04-17** 移除 BaoStock，统一 Tushare
- **2026-04-10** 信号绩效追踪 + 周报
- **2026-04-07** 模拟盘交易引擎（盘中实时）
- **2026-04-01** auto_evolve 月度自动进化
- **2026-03-23** XGBoost ML 选股模型
- **2026-03-15** 多因子打分 + ETF 轮动 + 实盘部署

> 当前项目状态参考 [`PROJECT_STATUS.md`](PROJECT_STATUS.md)
