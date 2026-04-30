# 8维度分析选股推荐 — 需求与实现方案

> 状态: 方案设计中，待完善后落实
> 创建: 2026-04-30

## 一、需求描述

在推荐股票时新增8个分析维度，为每只推荐股提供更全面的决策依据：

1. **盘面情况** — 当日涨跌、量比、振幅、换手率
2. **大盘情况** — 上证/深证/创业板指表现、市场情绪
3. **行业情况** — 所属行业当日表现、行业内排名
4. **利好** — 个股新闻情绪、催化剂识别
5. **量价关系** — 量价背离/放量突破/缩量回调
6. **资金流向** — 主力/超大单/大单资金动向
7. **业绩情况** — 营收增速、净利润、ROE、PE估值
8. **订单情况** — 五档盘口、买卖力量比、大单挂单

**额外输出**: 推荐持股天数、买入价格、预期卖出价格、止损价、风险收益比

---

## 二、可行性分析

| 维度 | 数据源 | 难度 | 新增外部依赖 | 预计耗时 |
|------|--------|------|-------------|---------|
| 盘面情况 | 已有(腾讯实时行情+本地SQLite) | 低 | 否 | 0s |
| 大盘情况 | **新增**(腾讯指数接口 sh000001/sz399001/sz399006) | 中 | 否 | 1s |
| 行业情况 | **新增**(东方财富板块接口) | 中 | 否 | 3-5s |
| 利好 | 已有(sentiment/analyzer.py 复用) | 低 | 否 | 3-5s |
| 量价关系 | 已有(因子+本地SQLite日线) | 低 | 否 | 0s |
| 资金流向 | 已有(fetch_capital_flow_batch) + 新增近5日历史 | 低 | 否 | 2s |
| 业绩情况 | **新增**(Tushare income/finindicator) | 高 | 是(Tushare Token) | 2-3s |
| 订单情况 | **新增**(东方财富五档盘口) | 中 | 否 | 1-2s |

**总预计耗时**: 15-25秒（并发获取可压缩到15秒内）

---

## 三、实现思路

### 3.1 架构 — 选股后增强模块

在 `get_stock_picks_live()` 选股完成后，独立执行8维度分析，不影响现有选股逻辑：

```
get_stock_picks_live()
  Step 1-5: 多因子计算 → ML预测 → 综合评分 → 选出Top N
  Step 6:   资金流向获取 (已有)
  Step 7:   8维度深度分析 (新增)
            ↓
     analysis/eight_dimensions.py
            ↓
     结果写入 reason_data["eight_dimensions"]
```

- 用 try/except 包裹，8维度分析失败不影响选股结果
- 每个维度独立降级：某维度数据获取失败 → 默认50分 + "数据不可用"标签

### 3.2 新建文件

```
analysis/__init__.py            — 模块初始化
analysis/eight_dimensions.py    — 8维度分析主模块 (~400行)
  - enrich_picks_with_dimensions(picks, factor_df)  — 对外入口
  - analyze_market_overview(symbol, realtime, daily_df)  — 盘面
  - analyze_macro_market()                                — 大盘
  - analyze_industry(symbol)                              — 行业
  - analyze_catalysts(symbol, name)                       — 利好
  - analyze_volume_price(symbol, daily_df)                — 量价
  - analyze_capital_flow_enhanced(symbol)                 — 资金(含历史)
  - analyze_financials(symbol)                            — 业绩
  - analyze_order_book(symbol)                            — 订单
  - calc_trade_suggestion(price, predicted_return, ...)   — 交易建议
```

### 3.3 修改文件

| 文件 | 修改内容 |
|------|---------|
| `data/fetcher.py` | +`fetch_index_realtime()`(大盘指数)、+`fetch_order_book()`(五档盘口)、+`fetch_capital_flow_history()`(近5日资金流) |
| `portfolio/allocator.py` | `get_stock_picks_live()` 新增 Step 7 调用8维度分析 |
| `portfolio/reason_text.py` | 扩展 `humanize_reason()` 展示8维度+交易建议 |
| `simulation/engine.py` | `_generate_next_plan()` 买入计划携带8维度数据 |
| `simulation/report.py` | 日报展示8维度分析 |
| `config/settings.py` | +`INDEX_CODES` 常量 |

---

## 四、各维度详细设计

### 维度1: 盘面情况 (0秒，纯已有数据)

**数据源**: `fetch_realtime_tencent_batch()` + SQLite近20日日线

**分析内容**:
- 当日涨跌幅、振幅、换手率
- 量比 = 当日成交量 / 5日均量 → 判断放量(>2)/缩量(<0.5)
- 涨跌停判断(≥9.9%)
- 盘面标签: "放量上涨"/"缩量下跌"/"涨停"等

**评分(基准50)**:
- 放量上涨 +20, 放量下跌 -15
- 温和上涨 +10, 温和下跌 -10
- 高换手(>5%) +5, 低换手(<1%) -5

### 维度2: 大盘情况 (1秒，腾讯指数接口)

**数据源(新增)**: 腾讯 `qt.gtimg.cn/q=sh000001,sz399001,sz399006`

**新增函数**: `fetch_index_realtime()` → 上证/深证/创业板指数实时涨跌幅

**分析内容**:
- 三大指数当日涨跌幅
- 市场状态: 普涨(三指数均涨)/普跌/分化
- 近20日趋势(从本地或API获取)

**评分(基准50)**:
- 上证涨>1%: +20, 涨0~1%: +10, 跌0~-1%: -10, 跌>1%: -20
- 普涨额外 +10, 普跌额外 -10

### 维度3: 行业情况 (3-5秒，东方财富板块接口)

**数据源(新增)**: 东方财富 `push2.eastmoney.com/api/qt/clist/get` (行业板块列表)

**新增函数**: `get_stock_industry(symbol)` → 查询个股所属行业
**新增函数**: `fetch_industry_performance(industry)` → 行业涨跌幅+资金流向

**分析内容**:
- 个股所属申万/东财一级行业
- 行业当日涨跌幅、近5日趋势
- 个股在行业内涨幅排名

**评分(基准50)**:
- 行业涨>2%: +20, 涨0~2%: +10, 跌: 对应减分
- 个股行业排名前20%: +15, 后20%: -15

### 维度4: 利好 (3-5秒，复用已有情绪分析)

**数据源**: 已有 `analyze_stock_sentiment(code, name)` → 情绪分数+新闻

**分析内容**:
- 复用现有情绪分析模块
- 新增催化剂关键词检测: 订单/中标/获批/回购/增持等
- 利好/利空新闻Top 3

**评分(基准50)**:
- 情绪>0.3: +20, <-0.3: -20
- 有明确催化剂: 每个+10, 上限+20

### 维度5: 量价关系 (0秒，纯本地计算)

**数据源**: SQLite本地日线数据

**分析内容**:
- 量价背离: 近5日价涨量缩 → 见顶风险
- 放量突破: 量显著放大+价破MA20
- 缩量回调: 价跌量缩 → 健康调整
- 量价配合度: 上涨放量+下跌缩量 → 良好

**评分(基准50)**:
- 放量上涨 +25, 放量下跌 -20
- 缩量上涨 +10, 缩量下跌 +5(正常调整)
- 量价背离(价涨量缩) -15

### 维度6: 资金流向 (2秒，已有接口+新增历史)

**数据源**: `fetch_capital_flow_batch()` + 新增 `fetch_capital_flow_history(symbol, days=5)`

**新增函数**: `fetch_capital_flow_history()` → 东方财富近N日资金流向

**分析内容**:
- 当日主力净流入/流出金额
- 超大单/大单/中单/小单明细
- 近5日主力资金累计方向
- 资金与股价方向是否一致

**评分(基准50)**:
- 主力净流入>5000万: +20, >1000万: +10
- 主力净流出>5000万: -20, >1000万: -10
- 近5日累计为正: +10

### 维度7: 业绩情况 (2-3秒，Tushare财务接口)

**数据源(新增)**: Tushare `income`(营收/净利润) + `finindicator`(ROE/毛利率)

**新增函数**: `fetch_stock_financials(symbol)` → 最新季度财务数据

**分析内容**:
- 营业收入同比增速
- 净利润同比增速
- ROE、毛利率
- PE估值水平(已有)
- 业绩预告(如有)

**评分(基准50)**:
- 营收同比>20%: +15, 0~20%: +5, <0: -10
- 净利润同比>20%: +15, 亏损: -20
- ROE>15%: +10, <5%: -10
- PE<15(低估值): +10, >50(高估值): -10

### 维度8: 订单情况 (1-2秒，东方财富五档盘口)

**数据源(新增)**: 东方财富 `push2.eastmoney.com/api/qt/stock/get` (含五档字段)

**新增函数**: `fetch_order_book(symbol)` → 五档买卖价量

**分析内容**:
- 买卖力量比 = (买一+买二量) / (卖一+卖二量)
- 大单挂单检测: 买/卖一档异常大单
- 买卖价差(spread)
- 盘口标签: "买盘强"/"卖压大"/"均衡"

**评分(基准50)**:
- 买力/卖力>2: +20, <0.5: -20
- 买一有大单(>10倍均量): +10(托单)
- 卖一有大单: -10(压单)

---

## 五、交易建议

### 持股天数推荐

基于 ML 预测收益 + 10日波动率动态推算:
- 预测收益>10% 且 波动率<3%: 15-20日 (长持)
- 预测收益>5% 且 波动率<5%: 10-15日 (中持)
- 预测收益>0%: 5-10日 (短持)

### 买入价格

当前实时价格（已有）

### 预期卖出价格

```
target_price = buy_price × (1 + predicted_return)    # ML预测
stop_price = buy_price × (1 - 0.08)                  # 固定止损-8%
support_price = max(MA20, 近20日最低价)               # 技术支撑
resistance_price = 近20日最高价                        # 技术阻力
risk_reward_ratio = expected_return / max_loss         # 风险收益比
```

---

## 六、数据结构设计

### reason_data 扩展

```python
reason_data = {
    # === 保留现有字段 ===
    "factor_rank": int,
    "ml_rank": int,
    "in_both": bool,
    "final_score": float,
    "dimension_scores": {...},
    "dimension_details": {...},
    "key_factors": {...},
    "predicted_return": float,
    "capital_flow": {...},

    # === 新增: 8维度分析 ===
    "eight_dimensions": {
        "盘面情况": {"score": 75, "label": "放量上涨", "change_pct": 0.032, "volume_ratio": 2.1, ...},
        "大盘情况": {"score": 65, "label": "偏多", "sh_change": 0.008, "market_state": "普涨", ...},
        "行业情况": {"score": 70, "label": "电子领涨", "industry": "电子", "industry_change": 0.023, ...},
        "利好":     {"score": 60, "label": "中性偏多", "sentiment": 0.3, "positive_news": [...], ...},
        "量价关系": {"score": 80, "label": "放量上涨", "vp_signal": "healthy", ...},
        "资金流向": {"score": 72, "label": "主力净流入", "net_mf_amount": 3200, "flow_5d": 12000, ...},
        "业绩情况": {"score": 68, "label": "稳健增长", "revenue_yoy": 0.15, "profit_yoy": 0.20, ...},
        "订单情况": {"score": 62, "label": "买盘略强", "buy_sell_ratio": 1.5, ...},
    },
    "eight_dim_score": 69,     # 8维度加权综合分

    # === 新增: 交易建议 ===
    "trade_suggestion": {
        "holding_days": 12,
        "buy_price": 15.32,
        "target_price": 17.05,
        "stop_price": 14.09,
        "support_price": 14.80,
        "resistance_price": 16.50,
        "expected_return": 0.113,
        "risk_reward_ratio": 2.5,
    },
}
```

---

## 七、展示格式

### 终端输出

```
华工科技(000988) 多因子#7、ML#3双重确认，得分72.3
  8维度 71分(偏多)
  盘面75 放量涨3.2% 量比2.1 | 大盘65 上证+0.8%普涨 | 行业78 电子+2.3%领涨
  利好60 中性偏多 | 量价82 放量突破 | 资金75 流入5200万 | 业绩68 营收+22%
  订单64 买力/卖力=1.8
  交易建议: 买入@28.35 → 目标31.50(+11%) 止损26.08 持有12日

  技术面75分(优)｜20日涨+17.2%(强势)，RSI=65，波动4.4%
  基本面85分(优)｜PE=11(低估值)，PB=0.8(破净)，换手率7.5%
  ... (保留现有维度展示)
```

### 微信推送(紧凑版)

```
**华工科技**(000988) 8维度71分
盘面75放量涨3.2% | 大盘65上证+0.8% | 行业78电子领涨
利好60偏多 | 量价82突破 | 资金75流入5200万 | 业绩68增长 | 订单64买盘强
买入@28.35 → 目标31.50(+11%) 止损26.08 持有12日
```

---

## 八、性能与降级策略

- **并发获取**: `ThreadPoolExecutor` 并发请求不同数据源
- **超时控制**: 每个维度API调用5秒超时，整体<30秒
- **渐进降级**: 任何维度失败 → 默认50分 + "数据不可用"标签
- **缓存**: 大盘指数/行业分类缓存1小时(日内不变)

---

## 九、待处理问题

### P0: 因子排名与ML预测权重分配不合理

**现状问题**:

当前评分公式 (`portfolio/allocator.py`):
```
final_score = 1/factor_rank × 100 + 1/ml_rank × 50 + in_both × 20
```

以 2026-04-30 推荐为例，好想你(002582):
- factor_rank=1 → 贡献 **100分**
- ml_rank=2394 → 贡献 **0.02分**
- in_both=false → 0分
- 结果: final_score=100.02，排名第1，分配44%仓位

**核心矛盾**: 因子排名压倒性主导，ML预测几乎无影响力。5只推荐股中3只ML预测为负（好想你-8%、贵州轮胎-3%、千味央厨-4.2%），但仍然被选为重仓。

**可行性方案（三选一）**:

#### 方案A: 分层过滤 + 独立评分（推荐）

```
第一层: 因子排名 → 取Top 50（不变）
第二层: ML预测硬过滤 → 排除预测 < -3% 的股票
第三层: 综合评分（新公式）
```

新评分公式:
```python
# 归一化到同一量级
factor_score = (1 / factor_rank) * 100          # #1→100, #10→10, #50→2
ml_score = max(0, (1 / ml_rank) * 100)          # #1→100, #10→10, 同量级
# ML预测方向加成/惩罚
if predicted_return > 0.03:   ml_score *= 1.2    # 强看多 +20%
elif predicted_return > 0:    ml_score *= 1.0    # 看多
elif predicted_return > -0.03: ml_score *= 0.5   # 弱看空 -50%
else:                         ml_score = 0       # 强看空 → 直接淘汰

final_score = factor_score × 0.5 + ml_score × 0.3 + in_both × 20
```

**效果模拟（2026-04-30数据）**:

| 股票 | 因子 | ML排名 | ML预测 | 旧得分 | 新得分 | 变化 |
|------|------|--------|--------|--------|--------|------|
| 好想你 | #1 | #2394 | -8.0% | **100.0** | 被过滤 | 淘汰 |
| 贵州轮胎 | #2 | #1948 | -3.0% | 50.0 | 被过滤 | 淘汰 |
| 轻纺城 | #3 | #524 | +0.8% | 33.4 | **56.8** | 升至#1 |
| 锦泓集团 | #4 | #261 | +1.6% | 25.2 | **47.1** | 升至#2 |
| 千味央厨 | #5 | #2159 | -4.2% | 20.0 | 被过滤 | 淘汰 |

优点: 简单直接，过滤掉ML强看空的股票
缺点: 阈值-3%需回测验证，可能过滤掉部分反弹股

#### 方案B: 连续惩罚（不过滤，用分数调节）

```python
# ML预测直接转为惩罚系数
ml_penalty = 1.0
if predicted_return > 0.05:   ml_penalty = 1.5    # 强看多加分
elif predicted_return > 0:    ml_penalty = 1.0     # 中性
elif predicted_return > -0.03: ml_penalty = 0.6    # 弱看空降权
elif predicted_return > -0.05: ml_penalty = 0.3    # 中度看空大幅降权
else:                         ml_penalty = 0.1     # 强看空极度降权

final_score = (1/factor_rank × 100) × ml_penalty + (1/ml_rank × 50) + in_both × 20
```

**效果模拟**:

| 股票 | 因子得分 | ml_penalty | 旧得分 | 新得分 |
|------|---------|------------|--------|--------|
| 好想你 | 100 | ×0.1 | 100.0 | **10.02** |
| 锦泓集团 | 25 | ×1.0 | 25.2 | **25.3** |
| 轻纺城 | 33 | ×1.0 | 33.4 | **33.5** |

好想你从100分降到10分，不再是第1重仓。

优点: 不硬性过滤，保留所有候选，仅调节权重
缺点: 仍然可能被选上（只是仓位变小）

#### 方案C: 多信号投票制

```python
# 三个维度各投票，少数服从多数
factor_bullish = factor_rank <= 20        # 因子看多
ml_bullish = predicted_return > 0         # ML看多
capital_bullish = net_mf_amount > 0       # 资金看多

# 至少2/3看多才入选
signals = [factor_bullish, ml_bullish, capital_bullish]
if sum(signals) < 2:
    exclude  # 信号不一致，跳过
```

**效果模拟**:

| 股票 | 因子 | ML | 资金 | 通过 |
|------|------|-----|------|------|
| 好想你 | ✓ | ✗ | ✗ | ✗ 淘汰 |
| 贵州轮胎 | ✓ | ✗ | ✓ | ✓ 2/3 |
| 轻纺城 | ✓ | ✓ | ✓ | ✓ 3/3 |
| 锦泓集团 | ✓ | ✓ | ✓ | ✓ 3/3 |
| 千味央厨 | ✓ | ✗ | ✓ | ✓ 2/3 |

优点: 直观易懂，多维度交叉验证
缺点: 逻辑偏简单，无法区分程度（+0.1%和+5%等价）

#### 建议

**优先落地方案A**（分层过滤），理由:
1. 改动最小 — 仅修改 `get_stock_picks_live()` 的 Step 5 评分逻辑
2. 风险可控 — ML预测<-3%本身意味着模型不看好，排除有依据
3. 效果明确 — 今天5只中3只会被过滤，剩余2只都是ML看多的
4. 可回测验证 — 用历史数据跑一遍，看过滤后胜率是否提升

后续可以叠加方案C的投票机制，在8维度分析中实现。

### P1: 业界因子+ML权重方法论调研（2026-04-30）

#### 核心认知：权重问题的本质

当前做法是"因子排名(人工权重) → ML排名 → 人工公式合并"，这是早期思路。
业界主流已转向 **"因子作为ML特征，模型自动学习权重"**：

```
pj_quant现状: 因子排名(人工2.0/1.5权重) + ML排名(人工公式) → final_score
业界主流:    因子作为特征 → ML模型(XGBoost/LightGBM)直接输出预测收益 → 排序
```

pj_quant 的 XGBoost 已经在做这件事（20个因子→预测20日收益），
问题在于选股时没把ML预测作为主决策依据，而是降级为辅助参考。

#### 业界主流参考项目

| 项目 | 方法 | 适用性 | 链接 |
|------|------|--------|------|
| **Qlib (微软)** | 因子→LightGBM/XGBoost预测收益→直接排序，无单独因子排名步骤 | 最成熟方案，架构参考 | [GitHub](https://github.com/microsoft/qlib) |
| **TIDIBEI** | XGBoost+GBDT+RandomForest多模型，ICIR加权合成因子后再预测 | 因子合成阶段用ICIR | [GitHub](https://github.com/JoshuaQYH/TIDIBEI) |
| **AShare-AI-Stock-Picker** | LightGBM+Optuna调参，直接用模型预测值排序 | 最接近的A股项目 | [GitHub](https://github.com/stlin256/AShare-AI-Stock-Picker) |
| **QuantsPlaybook** | 券商金工研报复现，IC/ICIR加权+滚动训练 | 权重方法论参考 | [GitHub](https://github.com/hugo2046/QuantsPlaybook) |
| **alphasickle** | 多因子全流程，沪深300增强，最大化IR分配权重 | 传统方法标杆 | [GitHub](https://github.com/phonegapX/alphasickle) |

#### 三种主流权重方法论

**方法1: IC/ICIR加权（传统但实用）**

每个因子按历史预测能力分配权重：
```python
IC_mean = 过去N期因子与收益相关系数的均值
IC_std  = 过去N期IC的标准差
ICIR    = IC_mean / IC_std

factor_weight = ICIR / sum(ICIR)    # 归一化
composite_score = sum(factor_i × weight_i)
```
- 优点: 可解释性强，因子贡献清晰
- 缺点: 只捕捉线性关系
- 参考: [全自动AI因子实战](https://zhuanlan.zhihu.com/p/1958363452742558077)

**方法2: ML端到端（Qlib方式，当前主流）**

```python
# 因子作为特征，模型直接预测收益
features = [mom_20d, pe_ttm, pb, vol_10d, ...]  # 20个因子
target = 未来20日收益率
model.fit(features, target)
predicted_return = model.predict(features)  # 直接用这个排序选股
```
- 优点: 捕捉非线性关系，自动学习因子权重
- 缺点: 黑盒，需要防过拟合
- 参考: [Qlib + LightGBM实战](https://vadim.blog/qlib-ai-quant-workflow-lightgbm)

**方法3: 多模型集成（Stacking/Blending）**

```python
# 第一层: 多个模型独立预测
pred_lgb = lgb_model.predict(features)
pred_xgb = xgb_model.predict(features)
pred_rf  = rf_model.predict(features)
# 第二层: 加权合并
final_pred = 0.4*pred_lgb + 0.35*pred_xgb + 0.25*pred_rf
```
- 参考: [量化金融面试题集](https://github.com/SoYuCry/awesome-quant-interview)

#### 对pj_quant的改进建议（按复杂度递增）

| 方案 | 改动量 | 思路 | 效果 |
|------|--------|------|------|
| **A. ML预测直接排序** | 最小 | 选股直接按 predicted_return 排序，去掉因子排名公式 | 从根本解决权重问题 |
| **B. ICIR加权因子合成** | 中等 | 用历史ICIR替换人工权重(动量2.0/估值1.5) | 让因子权重更科学 |
| **C. 多模型集成** | 较大 | 加LightGBM/RF，Blending后排序 | 提升预测稳定性 |

**如果只改一处，建议走方案A**：现有因子排名和ML模型做的事高度重叠
（都是用同样的20个因子），不如直接信任ML预测结果排序，因子排名降级为辅助展示。

#### 华泰证券研究启示

华泰金工对九坤Kaggle量化大赛的分析指出：
- 弱因子对神经网络有效但对XGBoost无效，弱因子权重不宜过大
- 因子合成和组合优化存在目标错配问题
- 参考: AlphaNet ([GitHub](https://github.com/ryanluoli1/AlphaNet))

---

## 十、实现顺序

1. 新建 `analysis/eight_dimensions.py` — 8维度分析骨架
2. 修改 `data/fetcher.py` — 新增3个数据获取函数(大盘/盘口/历史资金流)
3. 修改 `portfolio/allocator.py` — Step 7 集成8维度分析
4. **修改 `portfolio/allocator.py` — 重构评分公式（P0/P1联动）**
5. 修改 `portfolio/reason_text.py` — 展示格式
6. 修改 `simulation/engine.py` + `report.py` — 模拟盘集成
7. 修改 `config/settings.py` — 常量
