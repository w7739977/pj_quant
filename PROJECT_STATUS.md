# pj_quant 项目状态

> 单页快照 — 截至 2026-05-02。详细历史见 `PROGRESS.md`，部署见 `DEPLOY.md` / `docs/D_STRATEGY_DEPLOY.md`。

## 一、当前生产配置

| | |
|---|---|
| **策略** | D 方案 — 5 天频次共识选股 |
| **股票池** | 小盘 5~50 亿（剔除北交所/B 股，保留科创板）|
| **决策频率** | 每周 1 次（本周第一个交易日盘前 08:30 推送）|
| **持仓** | 10 只等权按 final_score 加权，硬持有到下一个共识日 |
| **ML 权重** | 50% ML + 50% 多因子（券商资管标准）|
| **总资金** | 50 万（INITIAL_CAPITAL）|
| **交易日感知** | `chinese_calendar` 包，节假日自动跳过/顺延 |

## 二、ML 模型

| | |
|---|---|
| 模型 | XGBoost 回归（20 日前瞻收益）|
| 因子数 | 23（动量 4 + 波动 2 + 换手 4 + 量价 3 + MA 3 + RSI 1 + 估值 2 + P0 财务 4）|
| 训练样本 | 110000+ |
| **R²** | **0.0719 ± 0.0042** |
| 训练数据 | 滚动截面 + 1%/99% winsorize |
| 中性化 | 默认禁用（`ENABLE_NEUTRALIZE=1` 启用，实测小盘策略下反而降低 R²）|
| 自动进化 | `python3 main.py evolve` 每月 1 号 16:00（含 4 周回测验证）|

模型文件**不在 git 仓库**（`.gitignore` 排除），新机器需 `evolve` 训练或 `scp` 复制。

## 三、回测验证

**4 个月窗口（2026-01-01 ~ 04-23, 13 周观测）**：

| 方案 | n | avg_alpha | 累计 alpha | sharpe-like | max_dd | 跑赢基准率 |
|---|---|---|---|---|---|---|
| A. 日频基线 | 71 | +0.41% | — | +0.15 | -5.40% | 52.8% |
| B. 周一快照 | 13 | +0.04% | +0.01% | +0.01 | -5.40% | 46.2% |
| C. 5 天信号平均 | 13 | +0.68% | +8.87% | +0.28 | -3.59% | 53.9% |
| **D. 5 天频次共识 (生产)** | **13** | **+1.15%** | **+15.69%** | **+0.50** | **-2.20%** | **69.2%** |

业界对标：4 个月 sharpe 0.5 → 年化 sharpe 1.3-1.6，接近成熟小盘多因子私募水平。

## 四、定时任务（云主机 crontab）

```cron
# 周本第一个交易日 08:30 — D 方案共识 picks 推送（节假日自动顺延）
30 8 * * 1-5  /opt/pj_quant/run_weekly.sh   >> logs/cron.log 2>&1

# 每个交易日 15:15 收盘后 — 持仓监控 + 缓存今日 scored
15 15 * * 1-5 /opt/pj_quant/run_daily.sh    >> logs/cron.log 2>&1

# 每月 1 号 16:00 — 自动进化（训练 + 上线 + 4 周回测验证）
0 16 1 * *    cd /opt/pj_quant && python3 main.py evolve --push >> logs/evolve.log 2>&1
```

## 五、关键代码模块

| 路径 | 职责 |
|---|---|
| `portfolio/consensus.py` | 5 天频次共识算法 + SQLite 缓存 |
| `portfolio/allocator.py` | 选股编排（live / consensus / monitor-only）|
| `portfolio/trade_utils.py` | 板块过滤 + 100 股整手 + 成本估算 |
| `ml/ranker.py` | XGBoost 训练 + 推理 + PIT 财务查询 |
| `ml/auto_evolve.py` | 月度自动进化 + 回测验证 |
| `factors/calculator.py` | 23 因子计算 + winsorize / 中性化 |
| `data/financial_indicator.py` | Tushare fina_indicator PIT 入库 |
| `analysis/eight_dimensions.py` | 8 维度选股深度分析 + AI 研判 |
| `sentiment/finbert_local.py` | FinBERT-Chinese 本地推理（市场情绪用）|
| `scripts/deploy.sh` | 一键部署（8 步） |
| `scripts/backtest_year.py` | 年度回测（4 套方案对比） |
| `scripts/backfill_consensus_cache.py` | 共识缓存冷启动回填 |
| `scripts/is_trading_day_check.py` | 交易日 / 周第一个交易日判断 |
| `scripts/validate_financial.py` | 财务数据 11 项质检 |

## 六、数据源

| 数据 | 来源 | 频率 |
|---|---|---|
| 行情 + daily_basic | Tushare | 每日增量 |
| 财务因子 PIT | Tushare fina_indicator | 季度 |
| 行业分类 | Tushare | 月度 |
| 市场情绪 | GLM-4-flash + FinBERT | 实时（仅展示）|
| 个股情绪因子 | sentiment_history | **暂未启用**（待回填）|

## 七、已知 limitation 与后续计划

- **样本量**：D 方案 13 周观测，统计力有限，建议 paper trading 1 个月验证
- **集中度**：top 10 同行业可能扎堆（4/20 单周 -2.48% 教训），待加行业限制
- **情绪因子**：`sentiment_history` 表未回填（Tushare news 限流），训练时禁用
- **触底回撤**：13 周中最大单周回撤 -2.20%，当前可接受

详见 `docs/optimization_backlog.md`。

## 八、文档导航

| 用途 | 文档 |
|---|---|
| **入门** | `README.md` |
| **本文档** | 项目当前状态快照 |
| 详细历史 | `PROGRESS.md` |
| 通用部署 | `DEPLOY.md` |
| **D 方案部署** | `docs/D_STRATEGY_DEPLOY.md` |
| 优化路线图 | `docs/optimization_backlog.md` |
| 历史决策 | `docs/archive/` |

## 九、一键部署

```bash
ssh user@cloud-host
cd /opt/pj_quant
bash scripts/deploy.sh           # 全量（含数据 + 训练 + 回测，~1.5 小时）
bash scripts/deploy.sh --quick   # 快速（仅代码 + 模型 + 缓存 + 冒烟，~10 分钟）
```

详见 `docs/D_STRATEGY_DEPLOY.md`。
