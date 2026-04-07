# A股量化系统 - 开发进度

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
