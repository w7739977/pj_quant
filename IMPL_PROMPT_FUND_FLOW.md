# 实现 Prompt — 主力资金流向因子 + A/B 离线回放

> 新分支：`feature/fund-flow-factor`（基于 main `ea541eb`）。  
> 目标：把"主力资金净流入"作为新因子加入选股决策，训练新 ML 模型，离线回放 2026-04-16 ~ 2026-04-26 与 main 模型对照。  
> 工程量约 2 小时（数据抓取 ~40min + 编码 ~60min + 训练/回放 ~20min）。

---

## 项目背景（精简）

- 当前 20 个因子覆盖动量/波动率/换手率/量价/技术/估值/情绪，**没有**主力资金流向
- 数据现状：本地 SQLite 有 K 线 + Tushare 基本面（pe_ttm/pb/turnover_rate/volume_ratio/total_mv），无资金流数据
- 选股入口：`portfolio.allocator.get_stock_picks_live` → `factors.calculator.compute_stock_pool_factors` → `ml.ranker.predict`
- A 股小盘策略对资金流向极敏感，研究表明"主力 5 日净流入率"与未来 20 日收益相关性 0.05-0.10
- 与 main 分支离线回放对比基准：本地 `logs/signals/` 仅 4 份（4-08, 4-25, 4-26 等），4-16 ~ 4-24 在云主机；故采用**双向离线回放**（同一脚本跑 main 模型与新模型，纯方法论 A/B）

---

## Phase 1：资金流数据获取（~40 分钟）

### 1.1 新建 `data/fund_flow_fetcher.py`

**接口**：AKShare `stock_individual_fund_flow(stock=code, market="sh"/"sz")`，单股调用一次性返回近 ~120 个交易日历史，13 列字段：

| AKShare 列名 | 落库字段 | 说明 |
|------|------|------|
| 日期 | date | YYYY-MM-DD |
| 收盘价 | close | 用于校验对账 |
| 涨跌幅 | pct_chg | % |
| 主力净流入-净额 | main_inflow | 元 |
| 主力净流入-净占比 | main_inflow_pct | % |
| 超大单净流入-净额 | super_large_inflow | 元 |
| 超大单净流入-净占比 | super_large_pct | % |
| 大单净流入-净额 | large_inflow | 元 |
| 大单净流入-净占比 | large_pct | % |
| 中单净流入-净额 | medium_inflow | 元 |
| 中单净流入-净占比 | medium_pct | % |
| 小单净流入-净额 | small_inflow | 元 |
| 小单净流入-净占比 | small_pct | % |

**SQLite Schema**（单表，所有股票同表带 code 列）：

```sql
CREATE TABLE IF NOT EXISTS fund_flow (
    code TEXT,
    date TEXT,
    close REAL,
    pct_chg REAL,
    main_inflow REAL,
    main_inflow_pct REAL,
    super_large_inflow REAL,
    super_large_pct REAL,
    large_inflow REAL,
    large_pct REAL,
    medium_inflow REAL,
    medium_pct REAL,
    small_inflow REAL,
    small_pct REAL,
    PRIMARY KEY (code, date)
);
CREATE INDEX IF NOT EXISTS idx_fund_flow_code ON fund_flow(code);
CREATE INDEX IF NOT EXISTS idx_fund_flow_date ON fund_flow(date);
```

**关键函数**：

```python
def fetch_one_stock(code: str) -> pd.DataFrame:
    """抓单股资金流，列名标准化为 schema 字段。失败返回空 DF。"""
    market = "sh" if code.startswith(("6", "5")) else "sz"
    df = ak.stock_individual_fund_flow(stock=code, market=market)
    # 重命名 + 类型转换 + code 列填充
    ...

def fetch_pool(codes: list, max_workers: int = 4) -> int:
    """并行抓多只股票，INSERT OR REPLACE 入库。返回成功数。"""
    # 用 ThreadPoolExecutor 4 并发，AKShare 接口允许，比串行快 3x
    ...

def get_last_fund_flow_date(code: str) -> str | None:
    """查询某股的最新 fund_flow 日期，用于增量"""
    ...

def load_stock_fund_flow(code: str, days: int = 60) -> pd.DataFrame:
    """读取某股最近 N 日资金流（供因子计算用）"""
    ...
```

### 1.2 抓取范围
- **只对小盘股池**（`get_small_cap_stocks(5e8, 5e10)`，~1500 只）— 新因子主要服务小盘策略
- 单股 ~1.5s，4 并发约 40 分钟全量
- 失败重试 1 次，最终失败的股票记录到 `logs/fund_flow_failed.json` 供后续补抓

### 1.3 入口命令
```bash
python3 -c "from data.fund_flow_fetcher import run; run()"
# 或限制测试
python3 -c "from data.fund_flow_fetcher import run; run(limit=10)"
```

**验收**：
- `sqlite3 data/quant.db "SELECT COUNT(DISTINCT code), COUNT(*) FROM fund_flow"` 应为 `(~1500, ~150000+)`
- 抽 3 只对账：`SELECT * FROM fund_flow WHERE code='000001' ORDER BY date DESC LIMIT 5` 与 AKShare 在线返回对比

---

## Phase 2：因子计算（~30 分钟编码）

### 2.1 在 `factors/calculator.py` 新增 `calc_fund_flow()` 函数

```python
def calc_fund_flow(symbol: str) -> dict:
    """
    资金流因子: 主力 5 日净流入率 + 超大单占比

    Returns
    -------
    {
        "main_net_inflow_5d": float,    # 5 日主力净流入占比均值（%）
        "super_large_ratio_5d": float,  # 5 日超大单占比均值（%）
    }
    """
    from data.fund_flow_fetcher import load_stock_fund_flow
    df = load_stock_fund_flow(symbol, days=10)
    if df.empty or len(df) < 5:
        return {"main_net_inflow_5d": np.nan, "super_large_ratio_5d": np.nan}
    recent = df.tail(5)
    return {
        "main_net_inflow_5d": float(recent["main_inflow_pct"].mean()),
        "super_large_ratio_5d": float(recent["super_large_pct"].mean()),
    }
```

### 2.2 在 `compute_all_factors()` 内调用

`factors/calculator.py:compute_all_factors` L182 增加：

```python
factors.update(calc_fund_flow(symbol))
```

放在 `calc_technical()` 之后、基本面因子之前。

### 2.3 同步加入截面训练用的 `prepare_training_data`

`ml/ranker.py:prepare_training_data` L99 之后增加：

```python
factors.update(calc_fund_flow(sym))  # 注意：用截面日 fund_flow，需截断到 end_idx
```

⚠️ **关键**：训练数据生成时 `calc_fund_flow` 默认读最近 5 日，但训练截面是历史某天，必须传 `end_date` 参数让 `load_stock_fund_flow` 截断到该日期。

修改 `load_stock_fund_flow(code, days, end_date=None)` 与 `calc_fund_flow(symbol, end_date=None)` 都加 `end_date` 参数支持。

---

## Phase 3：模型训练（~10 分钟）

### 3.1 `ml/ranker.py:FEATURE_COLS` 增加 2 列

```python
FEATURE_COLS = [
    "mom_5d", "mom_10d", "mom_20d", "mom_60d",
    "vol_10d", "vol_20d",
    "avg_turnover_5d", "avg_turnover_20d", "turnover_accel",
    "vol_price_diverge", "volume_surge",
    "ma5_bias", "ma10_bias", "ma20_bias", "rsi_14",
    "pe_ttm", "pb", "turnover_rate", "volume_ratio",
    "sentiment_score",
    # M6 新增
    "main_net_inflow_5d",
    "super_large_ratio_5d",
]
```

### 3.2 `strategy/small_cap.py:_score_stocks` 因子方向

```python
factor_direction = {
    ...,
    "main_net_inflow_5d": 1,    # 净流入越多越好
    "super_large_ratio_5d": 1,  # 超大单占比越高越好
}
# 给资金流因子加权重
elif "main_net" in factor_name or "super_large" in factor_name:
    weight = 1.5  # 与估值因子同等权重
```

### 3.3 训练命令

```bash
python3 main.py train
```

预期输出：
- `cv_r2_mean` 应 ≥ 旧模型（baseline R²=0.0902）
- 特征重要性中 `main_net_inflow_5d` / `super_large_ratio_5d` 应进入 top 10
- 模型保存到 `ml/models/xgb_ranker.json`，旧模型自动备份

⚠️ **如果新模型 R² 不如旧模型**：版本管理逻辑会自动把新模型保存为 `xgb_ranker_candidate.json`，不替换生产模型。回放时显式加载 candidate 即可。

---

## Phase 4：离线回放脚本（~30 分钟编码）

### 4.1 新建 `scripts/replay_picks.py`

**核心思路**：模拟"假设回到那天，会推荐什么"。用历史 close 价替代实时盘口价，跑 `get_stock_picks_live` 的等价逻辑。

```python
"""
离线回放选股推荐 — 给定日期，用历史数据模拟当日 get_stock_picks_live 输出

用法:
  python3 scripts/replay_picks.py --start 2026-04-16 --end 2026-04-26 \
      --model ml/models/xgb_ranker.json --output logs/replay/new_model/

  # 对照基准: 用旧模型再跑一遍
  python3 scripts/replay_picks.py --start 2026-04-16 --end 2026-04-26 \
      --model ml/models/xgb_ranker_baseline.json --output logs/replay/baseline/

输出: logs/replay/{tag}/{date}.json
  {
    "date": "2026-04-16",
    "picks": [
      {"code": "...", "factor_rank": N, "ml_rank": N, "in_both": bool,
       "final_score": ..., "predicted_return": ..., "key_factors": {...}}
    ]
  }
"""

def replay_one_day(date: str, model_path: str, top_n: int = 5) -> list:
    """
    回放单日选股
    
    1. 加载该日的小盘股池（用 latest_market_cap，但要按 date 截断 — 注意 stock_xxx 表也要按 date 过滤）
    2. compute_stock_pool_factors(end_date=date, skip_sentiment=True)
       - 注意 calc_fund_flow 也要传 end_date
    3. 加载指定 model_path 模型，跑 predict
    4. 多因子打分 + ML 排名 + 综合 final_score
    5. 取 top_n，组装 picks 返回（不查实时价、不算 shares 整手）
    """
    ...

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--model", default="ml/models/xgb_ranker.json")
    parser.add_argument("--output", default="logs/replay/")
    parser.add_argument("--top-n", type=int, default=5)
    args = parser.parse_args()
    
    import chinese_calendar
    from datetime import date as Date, timedelta
    
    d = Date.fromisoformat(args.start)
    end = Date.fromisoformat(args.end)
    while d <= end:
        if chinese_calendar.is_workday(d) and d.weekday() < 5:
            picks = replay_one_day(d.isoformat(), args.model, args.top_n)
            os.makedirs(args.output, exist_ok=True)
            with open(f"{args.output}/{d.isoformat()}.json", "w") as f:
                json.dump({"date": d.isoformat(), "model": args.model, "picks": picks}, f, ensure_ascii=False, indent=2)
            print(f"  {d.isoformat()}: 选出 {len(picks)} 只")
        d += timedelta(days=1)
```

### 4.2 关键改造：因子计算支持 end_date 截断

- `factors/calculator.compute_all_factors(symbol, end_date=...)` 已支持
- `factors/calculator.calc_fund_flow(symbol, end_date=None)` **需新增** end_date 参数
- `factors/calculator.compute_stock_pool_factors(end_date=...)` **传递给** calc_fund_flow

### 4.3 训练前先备份旧模型

在跑 `main.py train` 之前：

```bash
cp ml/models/xgb_ranker.json ml/models/xgb_ranker_baseline.json
```

让 baseline 模型固定为加新因子前的版本。新训练的模型作为 candidate。

---

## Phase 5：对比报告（~20 分钟）

### 5.1 新建 `scripts/compare_replays.py`

```python
"""
对比两组离线回放结果，输出差异表 + 重叠率 + 收益对比

用法:
  python3 scripts/compare_replays.py \
      --baseline logs/replay/baseline/ \
      --new logs/replay/new_model/ \
      --report logs/replay/comparison.md
"""

def overlap_rate(a: list, b: list) -> float:
    """两组 picks 的代码重叠率"""
    sa, sb = {p["code"] for p in a}, {p["code"] for p in b}
    return len(sa & sb) / max(len(sa | sb), 1)

def realized_return(pick_date: str, code: str, days: int = 5) -> float:
    """从本地 stock_xxx 读 pick_date 至 pick_date+N 的实际收益"""
    df = load_stock_daily(code)
    ...

def generate_report(baseline_dir, new_dir, output_md):
    """
    输出包含:
    1. 每日对比表: 日期 | baseline top5 | new top5 | 重叠率 | 新增/减少
    2. 实际收益对比: 每组 picks 的 5 日 / 20 日实际收益均值
    3. 特征重要性对比: 新模型新增的两个因子排名
    4. 总结: 新因子是否有效
    """
    ...
```

### 5.2 报告输出格式

`logs/replay/comparison.md`:

```markdown
# 资金流因子 A/B 对比报告 (2026-04-16 ~ 2026-04-26)

## 概览
- baseline 模型: R²=0.0902, 20 因子
- 新模型: R²=?, 22 因子（+ main_net_inflow_5d, super_large_ratio_5d）
- 回测日数: 8 个交易日
- 平均每日 top5 重叠率: ?%

## 每日对比

| 日期 | baseline top5 | 新模型 top5 | 重叠 |
|------|---------------|-------------|------|
| 2026-04-16 | ... | ... | 3/5 |
| ... | | | |

## 实际收益对比 (按 5 日 forward)

|  | baseline | 新模型 | 差异 |
|------|------|------|------|
| 平均收益 | +X.X% | +Y.Y% | +Z.Z% |
| 胜率 | XX% | YY% | |
| 最大回撤 | -X.X% | -Y.Y% | |

## 特征重要性变化

排名 | 因子 | 重要性 | 变化
1. mom_20d ...
N. main_net_inflow_5d (新) ...

## 结论
- [ ] 新因子 top 10 ✓/✗
- [ ] 新模型 R² 提升 ≥ 0.005 ✓/✗
- [ ] 实际收益均值提升 ≥ 0.5% ✓/✗
- [ ] 重叠率 ∈ [40%, 80%]（既要差异化又不能完全偏离）✓/✗

如全部 ✓ → 推荐合并到 main 替换生产模型。
```

---

## 整体执行顺序

```bash
# 1. 数据抓取（40min）
python3 -c "from data.fund_flow_fetcher import run; run()"

# 2. 备份 baseline
cp ml/models/xgb_ranker.json ml/models/xgb_ranker_baseline.json

# 3. 训练新模型（10min）
python3 main.py train

# 4. 双向离线回放
python3 scripts/replay_picks.py --start 2026-04-16 --end 2026-04-26 \
    --model ml/models/xgb_ranker_baseline.json --output logs/replay/baseline/
python3 scripts/replay_picks.py --start 2026-04-16 --end 2026-04-26 \
    --model ml/models/xgb_ranker.json --output logs/replay/new_model/

# 5. 生成对比报告
python3 scripts/compare_replays.py \
    --baseline logs/replay/baseline/ \
    --new logs/replay/new_model/ \
    --report logs/replay/comparison.md
```

---

## 验收清单

### 数据
- [ ] `sqlite3 data/quant.db "SELECT COUNT(DISTINCT code) FROM fund_flow"` ≥ 1400
- [ ] 抽 3 只对账，最新 3 日数据与 AKShare 在线一致
- [ ] `data/fund_flow_fetcher.load_stock_fund_flow("000001", 10)` 返回非空

### 因子
- [ ] `factors.calculator.compute_all_factors("000001")` 输出含 `main_net_inflow_5d` / `super_large_ratio_5d`
- [ ] `factors.calculator.compute_all_factors("000001", end_date="2026-04-15")` end_date 生效（与今日值不同）
- [ ] 训练样本中两列覆盖率 ≥ 80%

### 模型
- [ ] 新模型 cv_r2_mean ≥ 0.085（与 baseline 0.0902 ±5%）
- [ ] feature_importance 中两列均 > 0.02
- [ ] `xgb_ranker_baseline.json` 备份存在

### 回放
- [ ] `logs/replay/baseline/2026-04-16.json` ~ `2026-04-25.json` 共 ≥ 7 个文件（去除节假日）
- [ ] 每个文件含 5 个 picks，字段含 final_score / predicted_return / key_factors
- [ ] 同一日 baseline 与 new_model 的 picks 不完全相同（重叠率 < 100%）

### 对比
- [ ] `logs/replay/comparison.md` 生成成功
- [ ] 报告含每日对比表 + 实际收益对比 + 特征重要性
- [ ] 4 项结论 checkmark 已填

### 测试
- [ ] `pytest tests/ -q` 仍 64 项全过（不影响主流程）

---

## 提交规范

按 4 个 commit 拆分：

```
feat(data): 新增 fund_flow_fetcher 抓取主力资金流向数据

- AKShare stock_individual_fund_flow 接口包装
- SQLite fund_flow 单表存储 (code, date) 主键
- 4 并发抓取小盘股池（~1500 只 × 120 日）
- 增量更新支持
```

```
feat(factor): 加入主力资金流向因子（5 日净流入 + 超大单占比）

- factors/calculator.calc_fund_flow + load_stock_fund_flow 支持 end_date
- compute_all_factors / compute_stock_pool_factors 集成
- ml/ranker.FEATURE_COLS 加 2 列
- strategy/small_cap.factor_direction 加 2 项（方向 +1，权重 1.5x）
- 训练新模型: R²=?, top10 含 main_net_inflow_5d
```

```
feat(scripts): 新增离线回放脚本 replay_picks.py

- 给定日期范围，逐日模拟 get_stock_picks_live
- 历史 close 替代实时盘口价
- 因子计算用 end_date 截断（无未来数据泄露）
- JSON 输出每日 picks 含完整决策依据
```

```
chore: 新增对比脚本 + 回放对比报告

- scripts/compare_replays.py: 重叠率 + 实际收益对比 + 特征重要性
- logs/replay/comparison.md: 4-16 ~ 4-26 双向对比
```

每个 commit 末尾：
```
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 已知风险与回退

1. **AKShare 接口限流**：如批量抓取被封，改为串行 + sleep 1.5s，约 40min → 2h
2. **历史数据缺失**：AKShare 个股只返回近 ~120 日，回测日期太早会无法支持训练。当前 4-16 之前 100+ 个交易日够用
3. **模型不收敛**：如果新因子相关性低，R² 可能反而下降。版本管理会保护生产模型不替换；回放仍可用 candidate 验证
4. **重叠率过低**：如果 < 30% 说明新因子主导太强，不一定是好事，需谨慎评估
5. **实际收益对比不显著**：8 个交易日样本太少，结果可能噪声大。**仅作信号验证**，不作为合并到 main 的唯一依据；必要时延长回放周期到 30 天

---

## 实施完成后

- 推送到 `feature/fund-flow-factor` 远端
- 报告评估后决定是否 PR 合并到 main：
  - 全部 ✓ → 创建 PR，注明对比报告
  - 部分 ✗ → 保留分支，记录在 PROGRESS.md，作为后续优化方向
