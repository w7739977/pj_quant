# pj_quant - A股量化交易系统

2万小资金 A 股量化实盘系统，整合 **小盘多因子 + ML预测 + 情绪分析**。

## 系统架构

```
┌──────────────────────────────────────────────────────┐
│              激进实盘引擎 (live)                       │
│                                                      │
│  持仓检查 ──→ 止损/止盈/超时 ──→ 卖出信号            │
│  多因子+ML ──→ 板块过滤 ──→ 100股整手 ──→ 买入信号    │
│                                                      │
│  3只集中持仓 | 排除科创/北交所 | 精确到股数和金额      │
│  止损-8% | 止盈+15% | 超时20日调仓                    │
└──────────────────────────────────────────────────────┘
```

## 快速开始

```bash
# 一键部署（安装依赖 + 初始化 + 首次运行）
bash setup.sh

# 或手动安装
pip install -r requirements.txt
python main.py fetch        # 下载历史数据
python main.py train        # 训练ML模型
python main.py deploy       # 生成今日操作清单
```

## 命令一览

| 命令 | 说明 |
|------|------|
| `python main.py live [--push] [--simulate]` | **激进实盘**：持仓检查+选股+精确股数+推送 |
| `python main.py sim` | **模拟盘**：查看状态 |
| `python main.py sim --start [--push]` | 启动模拟盘常驻进程 |
| `python main.py sim --run-once [--push]` | 模拟盘单次执行 |
| `python main.py sim --report [--weekly]` | 模拟盘日报/周报 |
| `python main.py sim --history` | 模拟盘历史交易 |
| `python main.py sim --reset` | 重置模拟盘 |
| `python main.py portfolio` | 查看持仓（含实时盈亏） |
| `python main.py portfolio --buy CODE --shares N --price X` | 记录买入 |
| `python main.py portfolio --sell CODE --price X` | 记录卖出 |
| `python main.py portfolio --reset` | 重置为初始状态 |
| `python main.py deploy [--push] [--simulate]` | 标准部署（ETF+个股） |
| `python main.py backtest` | ETF轮动策略回测 |
| `python main.py smallcap` | 小盘多因子选股 |
| `python main.py sentiment` | 市场情绪分析 |
| `python main.py train` | 训练XGBoost模型 |
| `python main.py predict` | ML选股预测 |
| `python main.py fetch` | 下载历史数据 |
| `python main.py fetch-all [--limit N] [--refresh]` | 批量下载全A股票日线 |
| `python main.py evolve [--push]` | 模型自动进化 |
| `python main.py evolve-history` | 查看进化记录 |

## 项目结构

```
pj_quant/
├── main.py                    # CLI 入口
├── setup.sh                   # 一键部署脚本
├── run_daily.sh               # 每日定时任务（crontab）
├── run_pipeline.sh            # 一键流水线（数据→训练→部署）
├── run_monthly_evolve.sh      # 每月模型进化
├── requirements.txt           # Python 依赖
│
├── config/
│   ├── settings.py            # 全局配置（含API密钥）
│   └── settings.py.example    # 配置模板
│
├── data/
│   ├── fetcher.py             # 数据获取（东方财富/AKShare/BaoStock/腾讯/新浪）
│   ├── storage.py             # SQLite 存储管理
│   ├── bulk_fetcher.py        # BaoStock 批量行情入库（4417只）
│   ├── tushare_fundamentals.py     # Tushare 估值补全（当前方案）
│   └── fundamentals_parquet/       # Parquet 缓存目录
│
├── strategy/
│   ├── base.py                # 策略基类
│   ├── etf_rotation.py        # ETF动量轮动策略
│   └── small_cap.py           # 小盘多因子选股策略
│
├── factors/
│   ├── calculator.py          # 因子计算（20个因子，含情绪因子）
│   └── data_loader.py         # 股票池+行情数据加载
│
├── ml/
│   ├── ranker.py              # XGBoost选股模型 + 版本管理
│   ├── auto_evolve.py         # 自动进化（训练+对比+替换）
│   └── models/                # 模型文件目录
│
├── sentiment/
│   └── analyzer.py            # 双模型情绪分析（glm-4-flash + GLM-5）
│
├── portfolio/
│   ├── allocator.py           # 统一组合引擎 + 激进实盘部署
│   ├── tracker.py             # 持仓跟踪（实时盈亏/手动同步）
│   └── trade_utils.py         # 交易工具（板块过滤/股数/成本）
│
├── simulation/
│   ├── engine.py              # 模拟盘主引擎（常驻进程+定时调度）
│   ├── matcher.py             # 撮合器（涨跌停/T+1/止损止盈）
│   ├── trade_log.py           # 交易记录+每日快照（SQLite）
│   └── report.py              # 日报/周报+绩效统计
│
├── backtest/
│   └── engine.py              # 回测引擎
│
├── alert/
│   └── notify.py              # 微信推送（PushPlus）
│
├── scripts/
│   └── validate_data.py       # 数据验证脚本
│
└── tests/
```

## 核心模块

### 1. 激进实盘引擎 (portfolio/allocator.py)

每日操作清单生成，精确到股数和金额：
- **持仓检查** → 批量实时行情，自动检测止损(-8%)/止盈(+15%)/超时调仓(>20日)
- **板块过滤** → 排除科创板(688)、北交所(8xx/4xx)、B股
- **选股** → 多因子打分 ∩ ML预测排名，双重确认加分
- **精确下单** → 实时价格 + 100股整手 + 交易成本估算
- **simulate模式** → 先卖后买，实际资金计算，零偏差

### 1.5 模拟盘引擎 (simulation/)

自建模拟撮合，与实盘完全隔离，盘中真实行情价格交易：
- **盘中实时运行** → 09:30-14:58 每3分钟轮询真实行情撮合（午休暂停）
- **交易日判断** → chinesecalendar 精确排除法定节假日
- **撮合规则** → ask1/bid1成交 + 滑点 + 涨跌停/T+1/止损止盈
- **收盘自动推送** → 15:00结算后推送日报到微信，引擎自动退出
- **独立存储** → SQLite(sim_trading.db) + JSON持仓
- **绩效统计** → 日报/周报，胜率、最大回撤、夏普比率

### 2. 数据获取 (data/)

5级数据源自动降级，本地SQLite + Parquet缓存：
1. 东方财富（最快）
2. AKShare（最全）
3. BaoStock（无限制）
4. Tushare（基本面/估值，速度快，Parquet格式缓存）
5. 腾讯API（实时行情）/ 新浪API（盘口数据）

数据文件：
- `data/fetcher.py` — 实时数据获取（东方财富/AKShare/BaoStock/腾讯/新浪）
- `data/storage.py` — SQLite 存储管理
- `data/bulk_fetcher.py` — BaoStock 批量行情入库（4417只股票日线）
- `data/tushare_fundamentals.py` — Tushare 估值数据补全（Parquet → SQLite）

### 3. 情绪分析 (sentiment/analyzer.py)

双模型协作 + 多源新闻：
- glm-4-flash：批量情绪标注（快速、低成本）
- GLM-5：深度推理分析（慢、高质量）
- 新闻源：东方财富
- 权重：flash 70% + GLM-5 30%

### 4. ML模型 (ml/ranker.py)

XGBoost回归，20个因子（含情绪因子）：
- 滚动截面训练，5折交叉验证
- 自动版本管理：新模型R²更高则自动替换
- 因子重要性追踪

### 5. 自动进化 (ml/auto_evolve.py)

每月闭环迭代：
1. 获取旧模型基准
2. 更新股票池 + 行情
3. 滚动计算因子（含情绪）
4. 训练新模型 + 对比R²
5. 更优则上线，否则保留

## 定时任务

```bash
# 每日部署（周一至周五 15:30）
30 15 * * 1-5 /path/to/pj_quant/run_daily.sh >> /path/to/pj_quant/logs/daily.log 2>&1

# 每月进化（每月1号 16:00）
0 16 1 * * /path/to/pj_quant/run_monthly_evolve.sh >> /path/to/pj_quant/logs/evolve.log 2>&1
```

## ETF标的池

| 代码 | 名称 | 定位 |
|------|------|------|
| 510300 | 沪深300ETF | 大盘蓝筹 |
| 510500 | 中证500ETF | 中盘成长 |
| 159915 | 创业板ETF | 科技成长 |
| 513100 | 纳指100ETF | 海外配置 |
| 511010 | 国债ETF | 防御资产 |

## 风险提示

- 本项目仅供学习研究，不构成投资建议
- 量化交易不保证盈利，历史回测不代表未来表现
- 资金有风险，投资需谨慎
