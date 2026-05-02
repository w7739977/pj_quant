# pj_quant — A 股量化交易系统

> 50 万本金小盘多因子 + ML 量化系统。**周频共识选股**（D 方案）+ 盘前推送 + 节假日自动顺延。

## 当前状态

- 📊 [`PROJECT_STATUS.md`](PROJECT_STATUS.md) — 单页项目快照（强烈推荐先读）
- 📈 4 个月回测 13 周观测：D 方案 +1.15% 周均 alpha，累计 +15.69%，胜率 69.2%
- 🤖 模型 R² 0.0719（23 因子 XGBoost）

## 快速开始

```bash
# 部署到云主机（一键）
ssh user@cloud-host
cd /opt/pj_quant
bash scripts/deploy.sh           # 全量 ~1.5 小时
bash scripts/deploy.sh --quick   # 快速 ~10 分钟（仅代码 + 模型 + 缓存 + 冒烟）
```

详见 [`docs/D_STRATEGY_DEPLOY.md`](docs/D_STRATEGY_DEPLOY.md)。

## 命令速查

### 实盘选股 / 推荐

| 命令 | 说明 |
|---|---|
| `python3 main.py live --consensus [--push]` | **D 方案共识选股**（建议周一盘前）|
| `python3 main.py live --monitor-only [--push]` | 持仓监控 + 缓存 scored（建议周二~五）|
| `python3 main.py live [--push] [--simulate]` | 日频选股（保留作 fallback）|
| `python3 main.py portfolio` | 查看持仓 + 实时盈亏 |

### 模型训练 / 回测

| 命令 | 说明 |
|---|---|
| `python3 main.py evolve [--push]` | 自动进化（训练 + 上线 + 4 周回测验证）|
| `python3 main.py evolve-history` | 进化历史 |
| `python3 scripts/backtest_year.py` | 今年以来回测（A/B/C/D 4 套对比）|
| `python3 scripts/backfill_consensus_cache.py --days 10` | 共识缓存冷启动回填 |

### 数据 / 校验

| 命令 | 说明 |
|---|---|
| `python3 main.py fetch-all --incremental` | Tushare 增量拉取 |
| `python3 main.py fetch-financial` | 财务因子 PIT（季度）|
| `python3 main.py fetch-industry` | 行业分类（月度）|
| `python3 scripts/validate_financial.py` | 财务数据 11 项质检 |

## 关键设计

| 维度 | 选择 |
|---|---|
| 股票池 | 小盘 5~50 亿，剔除北交所/B 股，保留科创板 |
| 因子 | 23 个（动量 4 + 波动 2 + 换手 4 + 量价 3 + MA 3 + RSI 1 + 估值 2 + P0 财务 4）|
| ML 模型 | XGBoost 回归（20 日前瞻收益）|
| 综合得分 | `final_score = 0.5 × ML + 0.5 × 因子`（券商资管标准等权）|
| 选股策略 | **D 方案**：5 天 top-10 频次共识，过滤单日异动 |
| 决策频率 | 周频（本周第一个交易日盘前 08:30 推送）|
| 持仓 | 10 只等权按 final_score 加权 |
| 风控 | 个股 -8% 止损 / +15% 止盈 / 25 天超时调仓 |

## 项目结构

```
pj_quant/
├── PROJECT_STATUS.md              # 项目状态快照（先读这个）
├── README.md                      # 本文档
├── DEPLOY.md                      # 通用部署
├── PROGRESS.md                    # 近期开发进度
│
├── docs/
│   ├── D_STRATEGY_DEPLOY.md      # D 方案专项部署
│   ├── optimization_backlog.md   # 优化路线图
│   └── archive/                  # 历史决策档
│
├── main.py                        # CLI 入口
├── run_daily.sh                   # 收盘后 15:15（持仓监控 + 缓存）
├── run_weekly.sh                  # 周一盘前 08:30（共识选股推送）
│
├── config/settings.py             # 全局配置（API keys）
├── data/                          # 行情/估值/财务/情绪/行业 入库
├── factors/                       # 23 因子计算 + 中性化工具
├── ml/                            # XGBoost 训练 + 自动进化
├── portfolio/                     # 选股 + 共识算法 + 持仓追踪
├── analysis/eight_dimensions.py   # 8 维度选股分析
├── sentiment/finbert_local.py     # FinBERT 中文情绪推理
├── strategy/small_cap.py          # 小盘多因子打分
├── scripts/                       # 部署 / 回测 / 校验 / 工具
└── tests/                         # 99 个单测
```

## 提示

- ⚠️ 模型文件 `ml/models/*.json` 不在仓库（`.gitignore` 排除），需 `evolve` 训练或 `scp` 复制
- ⚠️ 共识缓存首次部署需 `backfill_consensus_cache.py` 一次性补足，否则下周一回退到日频
- ✅ 节假日自动跳过（`scripts/is_trading_day_check.py` + chinese_calendar）

## License

私有项目，仅供作者本人使用。
