# pj_quant - A股量化交易系统

50 万本金 A 股量化系统，**小盘多因子 + ML 预测 + 8 维度分析**。

## 系统架构

```
┌──────────────────────────────────────────────────────────┐
│                      生产链路                              │
│                                                          │
│  小盘股池 (5-50亿, ~2000 只)                              │
│       └── B股黑名单, 含科创/北交                          │
│                                                          │
│  26 个因子                                                │
│   ├── 价量类 (15): 动量/波动/换手/量价/技术              │
│   ├── 估值 (2): pe_ttm, pb                              │
│   ├── 活跃 (2): turnover_rate, volume_ratio             │
│   ├── 情绪 (1): sentiment_score（推送展示）             │
│   └── 财务 (4): ROE/营收增速/EPS增速/负债率 ← P0 新增   │
│                                                          │
│  XGBoost 预测 20 日收益                                   │
│  final_score = 0.7 × ML + 0.3 × 因子合成                 │
│                                                          │
│  推荐 10 只 + 8 维度展示 + 目标价/止损/风险收益比         │
│  止损 -8% | 止盈 +15% | 超时 20 交易日调仓               │
└──────────────────────────────────────────────────────────┘
```

## 快速开始

```bash
# 一键部署
bash setup.sh

# 或手动
pip install -r requirements.txt

# 数据初始化（按顺序）
python main.py fetch-all              # K 线 + 基本面（~30 分钟）
python main.py fetch-industry         # 行业分类
python main.py fetch-financial        # 财务指标 (P0 因子，~30 分钟)

# 训练 + 推荐
python main.py evolve                 # 训练新模型
python main.py live --simulate        # 生成今日推荐
```

## 命令一览

### 推荐 / 持仓

| 命令 | 说明 |
|------|------|
| `python main.py live [--push] [--simulate]` | 实盘推荐（含 8 维度分析）|
| `python main.py portfolio` | 查看持仓（含实时盈亏）|
| `python main.py portfolio --buy CODE --shares N --price X` | 记录买入 |
| `python main.py portfolio --sell CODE --price X` | 记录卖出 |
| `python main.py portfolio --reset` | 重置为初始状态 |

### 模拟盘

| 命令 | 说明 |
|------|------|
| `python main.py sim --start [--push]` | 模拟盘常驻进程（盘中撮合）|
| `python main.py sim --report [--weekly]` | 模拟盘日报/周报 |
| `python main.py sim --history` | 模拟盘历史交易 |
| `python main.py sim --reset` | 重置模拟盘 |

### 数据获取

| 命令 | 说明 |
|------|------|
| `python main.py fetch-all [--incremental]` | 全市场 K 线 + 基本面（Tushare）|
| `python main.py fetch-industry` | 行业分类 |
| `python main.py fetch-financial [--limit N]` | 财务指标（fina_indicator）|

### 模型 / 分析

| 命令 | 说明 |
|------|------|
| `python main.py train` | 训练 XGBoost |
| `python main.py predict` | ML 选股预测 |
| `python main.py evolve [--push]` | 模型自动进化 |
| `python main.py evolve-history` | 进化历史 |
| `python main.py sentiment` | 市场情绪分析 |
| `python main.py smallcap` | 小盘多因子选股 |
| `python main.py performance [--push]` | 信号绩效追踪 |

### ETF 旧版（仅供参考）

| 命令 | 说明 |
|------|------|
| `python main.py backtest` | ETF 轮动回测 |
| `python main.py deploy [--push]` | ETF + 个股标准部署 |

## 项目结构

```
pj_quant/
├── main.py                          # CLI 入口
├── README.md                        # 本文档
├── PROGRESS.md                      # 开发进度（含历次实验结论）
├── DEPLOY.md                        # 部署指南
│
├── config/settings.py               # 全局配置（含 API keys）
│
├── data/                            # 数据层
│   ├── fetcher.py                   # 实时行情（腾讯/东方财富/新浪）
│   ├── storage.py                   # SQLite 存储 + 市值汇总表
│   ├── tushare_daily.py             # K 线批量入库（按日期）
│   ├── tushare_fundamentals.py      # 估值数据补全
│   ├── tushare_industry.py          # 行业分类
│   ├── financial_indicator.py       # 财务指标 PIT 表 (P0)
│   ├── sentiment_history.py         # 情绪历史库 (代码就位，待数据)
│   ├── historical_news.py           # 历史新闻拉取（受 Tushare 权限限制）
│   └── sentiment_backfill.py        # FinBERT 批量回填
│
├── strategy/
│   ├── small_cap.py                 # 小盘多因子打分
│   └── etf_rotation.py              # ETF 轮动（旧版）
│
├── factors/
│   ├── calculator.py                # 22+ 因子计算 + 中性化工具（默认禁用）
│   └── data_loader.py               # 股票池 + 行情数据加载
│
├── ml/
│   ├── ranker.py                    # XGBoost + PIT 财务因子 + 26 FEATURE_COLS
│   ├── auto_evolve.py               # 自动进化（每月）
│   └── models/                      # 模型文件
│
├── sentiment/
│   ├── analyzer.py                  # GLM 双模型情绪分析（市场层）
│   └── finbert_local.py             # FinBERT 本地推理（个股层兜底）
│
├── analysis/
│   └── eight_dimensions.py          # 8 维度选股分析
│
├── portfolio/
│   ├── allocator.py                 # 选股引擎 + final_score 70/30
│   ├── tracker.py                   # 持仓跟踪
│   ├── reason_text.py               # 推送理由文案（结构化 dict）
│   └── trade_utils.py               # 交易工具（板块过滤/股数/成本）
│
├── simulation/
│   ├── engine.py                    # 模拟盘主引擎
│   ├── matcher.py                   # 撮合器
│   ├── trade_log.py                 # SQLite 交易日志
│   └── report.py                    # 日报/周报
│
├── backtest/engine.py               # 回测引擎
├── alert/notify.py                  # 微信推送
│
├── scripts/
│   ├── preflight.py                 # 健康检查
│   ├── postflight.py                # 信号归档
│   ├── financial_monthly.py         # 财务月度增量
│   ├── sentiment_daily.py           # 情绪日度增量
│   └── track_performance.py         # 绩效追踪
│
├── docs/
│   ├── strategy_explained.md        # 选股逻辑说明
│   ├── eight_dimensions_plan.md     # 8 维度设计
│   ├── optimization_backlog.md      # 优化待办
│   └── archive/                     # 历史 FIX_PROMPT 文档
│
└── tests/                           # 单元测试（74+ 项）
```

## 核心模块

### 1. 选股引擎 (portfolio/allocator.py)

每日推荐 10 只小盘股：
- **持仓检查** → 自动止损(-8%)/止盈(+15%)/超时调仓(20 交易日)
- **多因子打分** → 26 因子加权排名（财务因子 1.5x 权重）
- **ML 预测** → XGBoost 输出 20 日预测收益
- **综合排序**：`final_score = 0.7 × zscore(ML) + 0.3 × zscore(多因子)`
- **8 维度分析** → 盘面/大盘/行业/利好/量价/资金/业绩/订单
- **交易建议** → 目标价、止损价、风险收益比

### 2. ML 模型 (ml/ranker.py)

XGBoost 回归 + 时间序列交叉验证：
- 26 因子（22 价量基本面 + 4 财务 PIT）
- 滚动截面训练
- 自动版本管理（新模型 R² 更高才上线）
- PIT 数据正确性：财务因子按 `ann_date` 过滤，避免未来数据泄露

### 3. 数据底座 (data/)

- **K 线 + 基本面**：`tushare_daily` + `tushare_fundamentals`，全市场 5500+ 只
- **行业分类**：`tushare_industry` 110 个行业
- **财务指标**：`financial_indicator` PIT 表（roe_yearly/or_yoy/dt_eps_yoy/debt_to_assets 等）
- **实时行情**：腾讯/东方财富免费接口
- **市值汇总表**：`latest_market_cap` 毫秒级股票池筛选

### 4. 8 维度分析 (analysis/eight_dimensions.py)

每只推荐股展示 8 个维度评分：

```
盘面 75 分 | 量比 1.8(放量), 换手 6.2%
大盘 60 分 | 上证 +0.5%, 普涨
行业 80 分 | 纺织服装 +2.1%(强势)
利好 70 分 | 一季报增 28%
量价 65 分 | 5日量比 1.4x
资金 85 分 | 主力净流入 5800万
业绩 75 分 | PE=11(低估), ROE=15%
订单 60 分 | 买卖力量 58:42
```

## 定时任务

```bash
# 每日 15:30 推送（周一至周五）
30 15 * * 1-5 /path/to/pj_quant/run_daily.sh

# 模拟盘 09:05 启动（盘中撮合，15:00 自动结算推送）
5 9 * * 1-5 /path/to/pj_quant/run_sim_daily.sh

# 每月 1 号 16:00 模型进化
0 16 1 * * /path/to/pj_quant/run_monthly_evolve.sh

# 每月 1 号 17:00 财务指标增量
0 17 1 * * cd /path/to/pj_quant && python3 scripts/financial_monthly.py
```

## 关键实证结论

详见 `PROGRESS.md` 2026-05-02 章节，简要：

1. **业界中性化在小盘策略下失效**：滚动截面密度太低，rank/zscore 退化失真。代码保留默认禁用。
2. **全市场池对当前因子库不可行**：22 因子撑不住板块差异，R² 从 0.0757 崩到 0.0022。
3. **情绪因子在 ML 训练中贡献为零**：训练历史无新闻 → NaN → 模型学不到。FinBERT 本地推理仅用于推送层。

## 风险提示

- 本项目仅供学习研究，不构成投资建议
- 量化交易不保证盈利，历史回测不代表未来表现
- 资金有风险，投资需谨慎
