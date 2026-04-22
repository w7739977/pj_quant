# A股量化系统 - 开发进度

## 2026-04-22 因子计算卡死 + GLM-5 静默失败修复

### 背景

每日15:30 cron执行 `run_daily.sh` → `live --push`，当天推送未收到。排查发现进程卡在因子计算环节37分钟+，日志大量 `Broken pipe` 和 `服务器连接失败`。

### 问题一：因子计算网络 fallback 导致卡死

**根因**：`factors/calculator.py` 中 `compute_stock_pool_factors()` 对2868只股票逐个调用 `get_stock_daily()`，本地SQLite无缓存的股票会 fallback 到 BaoStock/AKShare，收盘后这些网络请求大量超时失败，每个阻塞数秒，累积导致整个流程卡死。此外 `get_stock_fundamentals()` 也会调用腾讯批量API获取基本面数据，收盘后同样不稳定。

**修改文件**：`factors/calculator.py`

**改动1 — `compute_all_factors()` 改为纯本地读取**

```python
# 修改前：
from factors.data_loader import get_stock_daily, get_stock_fundamentals, get_small_cap_stocks

def compute_all_factors(symbol: str, end_date: str = None, lookback: int = 120) -> dict:
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=lookback * 2)).strftime("%Y-%m-%d")

    df = get_stock_daily(symbol, start, end_date)  # ← 会 fallback 到 BaoStock
    if df is None or len(df) < 20:
        return {}

    factors = {"code": symbol}
    factors.update(calc_momentum(df))
    factors.update(calc_volatility(df))
    factors.update(calc_turnover_factor(df))
    factors.update(calc_volume_price(df))
    factors.update(calc_technical(df))
    return factors
```

```python
# 修改后：
from factors.data_loader import get_stock_daily, get_stock_fundamentals, get_small_cap_stocks
from data.storage import load_stock_daily  # ← 新增：直接读本地SQLite

def compute_all_factors(symbol: str, end_date: str = None, lookback: int = 120) -> dict:
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=lookback * 2)).strftime("%Y-%m-%d")

    # 直接读本地SQLite，不做网络fallback
    df = load_stock_daily(symbol)
    if df is None or df.empty or len(df) < 20:
        return {}
    df = df[(df["date"] >= start) & (df["date"] <= end_date)]
    if len(df) < 20:
        return {}

    factors = {"code": symbol}
    factors.update(calc_momentum(df))
    factors.update(calc_volatility(df))
    factors.update(calc_turnover_factor(df))
    factors.update(calc_volume_price(df))
    factors.update(calc_technical(df))

    # 基本面因子：直接从本地SQLite读取，避免收盘后调用腾讯API
    last_row = df.iloc[-1]
    for col in ["pe_ttm", "pb", "turnover_rate", "volume_ratio"]:
        factors[col] = last_row.get(col, np.nan)

    return factors
```

**改动2 — `compute_stock_pool_factors()` 去掉网络调用**

```python
# 修改前：
    # 基本面因子（批量获取，高效）
    fund = get_stock_fundamentals(symbols)          # ← 调用腾讯批量API
    fund_dict = {}
    if not fund.empty:
        for _, row in fund.iterrows():
            fund_dict[row["code"]] = row.to_dict()

    all_factors = []
    for i, sym in enumerate(symbols):
        try:
            f = compute_all_factors(sym, end_date)
            if f:
                # 合并基本面
                fd = fund_dict.get(sym, {})
                f["pe_ttm"] = fd.get("pe_ttm", np.nan)
                f["pb"] = fd.get("pb", np.nan)
                f["market_cap"] = fd.get("market_cap", np.nan)
                f["turnover_rate"] = fd.get("turnover_rate", np.nan)
                f["volume_ratio"] = fd.get("volume_ratio", np.nan)
                all_factors.append(f)
```

```python
# 修改后（基本面因子已在 compute_all_factors 中从本地读取）：
    # 逐只计算因子（基本面因子已从本地SQLite读取，无需网络请求）
    all_factors = []
    for i, sym in enumerate(symbols):
        try:
            f = compute_all_factors(sym, end_date)
            if f:
                all_factors.append(f)
```

### 问题二：GLM-5 思考模型 max_tokens 耗尽导致静默失败

**根因**：GLM-5 是深度思考模型，`reasoning_tokens` 计入 `max_tokens` 预算。原来设置 `max_tokens=2000`，全部被推理过程消耗，`content` 始终为空字符串。API返回200但实际输出为空，`_call_llm()` 返回空串，GLM-5 深度分析一直静默失败，情绪分析只靠 glm-4-flash 单模型（70%权重）。

**验证**：
```
max_tokens=200:  reasoning_tokens=199,  content='' (全被思考消耗)
max_tokens=500:  reasoning_tokens=499,  content='' (全被思考消耗)
max_tokens=8000: reasoning_tokens=1679, content=完整JSON (正常)
```

**修改文件**：`sentiment/analyzer.py`

```python
# 修改前：
    content = _call_llm("glm-5", prompt, max_tokens=2000, temperature=0.3, timeout=120)

# 修改后：
    content = _call_llm("glm-5", prompt, max_tokens=8000, temperature=0.3, timeout=180)
```

### 修改文件汇总

| 文件 | 变更 |
|------|------|
| `factors/calculator.py` | `compute_all_factors()` 改用 `load_stock_daily()` 纯本地读取，新增基本面因子提取；`compute_stock_pool_factors()` 去掉 `get_stock_fundamentals()` 网络调用 |
| `sentiment/analyzer.py` | GLM-5 `max_tokens` 2000→8000，timeout 120→180 |

### 效果对比

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 因子计算耗时 | 37分钟+卡死 | 3分36秒完成 |
| Broken pipe 错误 | 大量 | 0 |
| GLM-5 深度分析 | 静默失败（content为空） | 正常返回JSON |
| 推送 | 未送达 | 正常送达微信 |

### 运维操作

- 清理 root crontab 中5个 openclaw 定时任务，只保留 stargate
- 终止2个 openclaw stock_monitor 常驻进程

---

## 2026-04-17 模拟盘交易引擎

### 背景

原项目是收盘后选股 → T+1 手动同步操作。现在新增自建模拟撮合引擎，实现全流程自动化模拟交易。

### 设计方案

**盘中实时交易模式:**
```
cron 09:05 触发 → 判断交易日(chinesecalendar) → 启动引擎
  09:25  盘前准备: 加载昨日选股计划 → 生成今日订单
  09:30  开盘: 每3分钟拉取实时行情 → 真实价格撮合 → 止损/止盈监控
  11:30  午休暂停
  13:00  下午盘继续轮询
  14:58  停止盘中轮询
  15:00  收盘结算 → 每日快照 → 生成明日计划 → 推送日报微信 → 引擎自动退出
```

**撮合规则:**
- 市价买入: ask1 + 滑点 (0.01元)
- 市价卖出: bid1 - 滑点
- 涨停板: 无法买入 / 跌停板: 无法卖出
- T+1: 当日买入不可当日卖出
- 100股整手

**交易日判断:**
- 使用 `chinesecalendar` 库精确排除周末+法定节假日（劳动节/国庆/春节等）
- `run_sim_daily.sh` 入口处判断，非交易日直接跳过

**交易时间校验:**
- 所有交易操作必须在 09:30-14:58 盘中执行，使用当时真实行情价格
- `run_once` 模式仅供离线调试，强制不推送
- 收盘后只做结算/快照/推送，不再撮合新订单

### 新增文件

| 文件 | 职责 |
|------|------|
| `simulation/__init__.py` | 模块入口 |
| `simulation/matcher.py` | 撮合器: 买卖撮合、涨跌停检查、止损/止盈实时触发、T+1规则 |
| `simulation/engine.py` | 主引擎: 常驻进程、APScheduler式定时调度、订单管理 |
| `simulation/trade_log.py` | 交易记录持久化 (SQLite独立库 sim_trading.db) + 每日快照 + 持仓管理 |
| `simulation/report.py` | 日报/周报生成 + 微信推送格式化 + 绩效统计 (胜率/回撤/夏普) |

### 修改文件

| 文件 | 变更 |
|------|------|
| `main.py` | 新增 `sim` 命令 (start/run-once/report/history/reset) |
| `config/settings.py` | 新增模拟盘参数 (SIM_INITIAL_CAPITAL/SIM_DB_PATH/SIM_BAR_INTERVAL) |

### 命令用法

```bash
python main.py sim                    # 查看模拟盘状态
python main.py sim --start [--push]   # 启动常驻进程
python main.py sim --run-once [--push]# 单次执行（测试）
python main.py sim --report           # 当日报告
python main.py sim --report --weekly  # 周报（胜率/回撤/夏普）
python main.py sim --history          # 历史交易记录
python main.py sim --reset            # 重置（清空持仓+数据库）
```

### 数据存储

模拟盘与实盘完全隔离:
- **SQLite**: `data/sim_trading.db` (sim_orders / sim_trades / sim_snapshots)
- **持仓**: `data/sim_portfolio.json`
- **计划**: `data/sim_daily_plan.json`

### 与现有模块的关系

| 现有模块 | 复用方式 |
|---------|---------|
| `portfolio/tracker.py` | 复用思路，模拟盘独立持仓实例 |
| `portfolio/allocator.py` | 复用 `get_stock_picks_live()` 选股 |
| `portfolio/trade_utils.py` | 复用手续费计算、板块过滤、股数计算 |
| `data/fetcher.py` | 复用 `fetch_realtime_tencent_batch()` 行情 |
| `alert/notify.py` | 复用 PushPlus 微信推送 |
| `ml/ranker.py` | 复用 ML 预测 |

### 自测结果

- 重置 → 空仓 → 生成计划(5只) → 第二轮买入成交5只 → 持仓正确 → 快照正确
- 止损/止盈/T+1/涨跌停逻辑验证通过
- `--status` / `--history` / `--report` / `--report --weekly` 输出正确
- 57个原有测试全部通过

### 已知限制

- 行情通过腾讯接口获取（无五档盘口），bid1/ask1 用当前价模拟
- 调休上班日 `chinesecalendar.is_workday()=True`，但交易所不开盘（极少见）

---

## 2026-04-08 激进实盘部署

### 模型训练完成

**XGBoost 模型 (R²=0.0902):**
- 训练样本: 96,246 条 (1,518 只股票)
- 交叉验证 R²: 0.0902 ± 0.0119
- 基本面因子修复生效: turnover_rate 排名 #2, pb 排名 #10
- 并行训练: 4 chunk 并行数据准备 → 288,034 条样本 → 合并训练

**Top 5 因子重要性:**
1. mom_20d (0.1261)
2. turnover_rate (0.1064)
3. vol_10d (0.1019)
4. ma10_bias (0.0853)
5. avg_turnover_5d (0.0617)

### 激进实盘策略

**配置:**
- 初始资金: 20,000 元
- 持仓数量: 3 只集中持仓
- 板块限制: 主板+创业板（排除科创板688/北交所8xx/4xx）
- 止损/止盈: -8% / +15%
- 最大持仓: 20 个交易日

**新增文件:**
- `portfolio/trade_utils.py` — 交易工具（板块过滤/股数计算/成本估算/清单格式化）
- `train_parallel.sh` — 并行训练脚本
- `ml/parallel_prepare.py` — 并行数据准备

**修改文件:**
- `portfolio/allocator.py` — 新增 `run_live_deploy()` 激进实盘部署
  - simulate 模式: 先卖后买，用实际回笼资金选股，资金零偏差
  - `check_holdings()` 止损/止盈/超时调仓检测
  - `get_stock_picks_live()` 快速选股（跳过情绪因子，~40秒）
- `portfolio/tracker.py` — 增强持仓管理
  - `get_realtime_summary()` 实时盈亏
  - 加仓均价自动合并
  - 卖出验证（不存在的股票提示错误）
- `main.py` — 新增 `live` 命令 + 增强 `portfolio` 命令
- `config/settings.py` — 新增实盘参数
- `factors/calculator.py` — `skip_sentiment` 参数，快速路径
- `data/storage.py` — 修复 `save_portfolio` 原地修改 bug

**命令用法:**
```bash
# 生成今日操作清单
python main.py live

# 推送到微信
python main.py live --push

# 推送 + 模拟执行（精确资金计算）
python main.py live --push --simulate

# 手动同步持仓
python main.py portfolio --buy CODE --shares N --price X
python main.py portfolio --sell CODE --price X
python main.py portfolio --reset
```

---

## 2026-04-07 Tushare 数据补全 + 模型训练

## 2026-04-07 晚间更新（Tushare 方案）

### 数据补全方案对比

| 对比项 | BaoStock | Tushare |
|--------|----------|---------|
| 获取方式 | 逐股票查询（每只3-5s） | 按日期批量（每天0.3s，5486只） |
| 全量耗时 | 预计 5 小时 | **22 分钟** |
| 失败率 | 较高（并发不支持） | **0/1515 失败** |
| 额外字段 | pe_ttm, pb, ps_ttm, pcf_ncf_ttm | pe_ttm, pb, ps_ttm, **turnover_rate, volume_ratio, total_mv** |
| 存储格式 | 直接写入 SQLite | **Parquet → SQLite（可复用）** |

### 当前数据完备状态

**SQLite 数据列（4417 只股票）：**
`date, open, high, low, close, volume, turnover, pct_chg, pe_ttm, pb, ps_ttm, pcf_ncf_ttm, turnover_rate, volume_ratio`

**Parquet 缓存：** `data/fundamentals_parquet/` (1515 文件, 414.8 MB)

**20 因子覆盖：18/20 就绪，仅 sentiment_score 需实时获取**

| 因子 | 来源 | 覆盖率 |
|------|------|--------|
| mom_5/10/20/60d | close 计算 | 100% |
| vol_10/20d | close 计算 | 100% |
| avg_turnover_5/20d, turnover_accel | turnover 计算 | 100% |
| vol_price_diverge, volume_surge | volume+close | 100% |
| ma5/10/20_bias, rsi_14 | close 计算 | 100% |
| pe_ttm | Tushare | 99.8% |
| pb | Tushare | 100% |
| turnover_rate | Tushare | 100% |
| volume_ratio | Tushare | 100% |
| sentiment_score | GLM-4-flash 实时 | 需部署时获取 |

### 待完成

- [x] XGBoost 模型训练（R²=0.0902, 96246样本）
- [x] 激进实盘部署 (`python main.py live`)
- [x] 持仓管理 + 手动同步
- [ ] 实盘跟踪验证（需观察1-2周信号准确度）

---

## 2026-04-06 ~ 04-07 工作记录

### 一、修复 BaoStock 数据获取 Bug

**文件:** `ml/auto_evolve.py`, `factors/data_loader.py`

| 行号 | 原代码 | 问题 | 修复 |
|------|--------|------|------|
| auto_evolve.py:88 | `row[5] == "1"` | row[5]是上市状态，不是类型 | → `row[4] == "1"`（type=股票）|
| auto_evolve.py:90 | 只判断 row[5] | 没过滤 type，指数/债券混入 | 需同时 `row[4]=="1" 且 row[5]=="1"` |
| auto_evolve.py:91 | `row[1].split(".")[-1]` | row[1]是名称不是代码 | → `row[0].split(".")[-1]` |
| data_loader.py:89 | `row[5] == "1"` | 同上 | → `row[4] == "1"` |

BaoStock `query_stock_basic()` 实际字段顺序（实测确认）:

| index | 字段 | 示例 |
|-------|------|------|
| 0 | code | sh.000001 |
| 1 | name | 上证综合指数 |
| 2 | ipoDate | 1991-07-15 |
| 3 | outDate | '' |
| 4 | type | 1=股票, 2=指数 |
| 5 | tradeStatus | 1=上市 |

---

### 二、批量行情数据获取（已完成）

**新增文件:** `data/bulk_fetcher.py`

- BaoStock 持久连接，4417 只股票日线批量入库
- 断点续传：已缓存股票自动跳过
- 连接中断自动重连
- 实际执行结果：**4416 只成功，0 失败，耗时 206 分钟**

```bash
python3 main.py fetch-all              # 全量拉取
python3 main.py fetch-all --limit 100  # 调试用
```

**数据验证结果（100%匹配）：**

| 股票 | 本地收盘价 | BaoStock在线 | 差异 |
|------|-----------|-------------|------|
| 000001 平安银行 | [10.99, 11.08, 11.15, 11.27, 11.12] | [10.99, 11.08, 11.15, 11.27, 11.12] | 0.0000 |
| 600519 贵州茅台 | [1420.0, 1450.0, 1459.44, 1459.88, 1460.0] | 同上 | 0.0000 |
| 300750 宁德时代 | [413.0, 401.7, 405.71, 401.17, 386.46] | 同上 | 0.0000 |

---

### 三、估值数据补全（已完成）

**方案一 (BaoStock):** `data/supplement_fundamentals.py`
- 速度慢：0.2只/s，全量预计5小时
- 已完成 1568/4417 后切换方案

**方案二 (Tushare, 最终采用):** `data/tushare_fundamentals.py`
- 按日期批量获取全市场 daily_basic，每次 0.3s 获取 5486 只股票
- 保存为 Parquet → 合并后批量 UPDATE SQLite
- **执行结果：1515 日期全部成功，0 失败，耗时 22 分钟**
  - 下载: 18 分钟 (1515 Parquet 文件, 414.8 MB)
  - 入库: 3.5 分钟 (4417 只股票)
  - 对比 BaoStock 方案快 14 倍

**数据验证结果：**

| 字段 | 股票覆盖率 | 数据行覆盖率 |
|------|-----------|-------------|
| pe_ttm | 99.8% (4409/4417) | 82.7% |
| pb | 100% (4417/4417) | 99.6% |
| ps_ttm | 100% | 100% |
| turnover_rate | 100% | 99.9% |
| volume_ratio | 100% | 99.9% |

完成后数据列：`date, open, high, low, close, volume, turnover, pct_chg, pe_ttm, pb, ps_ttm, turnover_rate, volume_ratio`

---

### 四、因子数据覆盖分析

模型训练需要 20 个因子，数据源覆盖：

| 类型 | 因子 | 数量 | 数据来源 | 状态 |
|------|------|------|---------|------|
| 动量 | mom_5/10/20/60d | 4 | close 计算 | ✓ 已有 |
| 波动率 | vol_10/20d | 2 | close 计算 | ✓ 已有 |
| 换手率 | avg_turnover_5/20d, turnover_accel | 3 | turnover 列 | ✓ 已有 |
| 量价 | vol_price_diverge, volume_surge | 2 | volume+close | ✓ 已有 |
| 技术 | ma5/10/20_bias, rsi_14 | 4 | close 计算 | ✓ 已有 |
| 基本面 | pe_ttm, pb | 2 | Tushare daily_basic | ✓ 已有(100%) |
| 基本面 | turnover_rate, volume_ratio | 2 | Tushare daily_basic | ✓ 已有(100%) |
| 情绪 | sentiment_score | 1 | GLM-4-flash | 需实时 |

---

### 五、其他修改

**新增文件:**
- `data/bulk_fetcher.py` — 批量行情获取
- `data/supplement_fundamentals.py` — BaoStock 估值补全（已弃用）
- `data/tushare_fundamentals.py` — Tushare 估值补全（最终方案）
- `run_pipeline.sh` — 一键流水线（数据→训练→部署）
- `scripts/validate_data.py` — 数据验证脚本
- `.gitignore` — 排除密钥/数据库/日志
- `config/settings.py.example` — 配置模板（不含密钥）

**修改文件:**
- `data/storage.py` — 新增 save_stock_daily/load_stock_daily/list_cached_stocks
- `data/loader.py` → `factors/data_loader.py` — 修复 BaoStock bug，优先读本地缓存
- `ml/auto_evolve.py` — 修复字段索引，去掉500只限制
- `ml/ranker.py` — prepare_training_data 改为滚动截面生成（纯本地）
- `factors/calculator.py` — 修复情绪因子后覆盖DataFrame的bug
- `main.py` — 新增 fetch-all、deploy 命令
- `README.md` — 全面更新
- `setup.sh` — 一键部署脚本
- `run_daily.sh` — 改为统一 deploy

**GitHub 仓库:**
- https://github.com/w7739977/pj_quant （公开）
- 已推送 2 个 commit（初始提交 + BaoStock修复+批量获取）

---

### 六、待完成

- [x] 估值数据补全完成（Tushare 方案，4417 只 100% 覆盖）
- [x] 补全后全量数据验证（pe_ttm/pb/ps_ttm/turnover_rate/volume_ratio 全部就绪）
- [ ] XGBoost 模型训练
- [ ] 首次 deploy 生成操作清单
- [x] volume_ratio 因子补充（Tushare daily_basic 已包含）
