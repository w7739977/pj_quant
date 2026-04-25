# 代码修复 Prompt

> 用于把这份 prompt 喂给另一个 AI 编辑会话或开发者，按优先级闭环修复 review 中发现的问题。  
> 修复顺序严格按 Phase 1 → 2 → 3，每个 Phase 内部按编号顺序。

---

## 项目背景

- A 股量化系统，2 万小资金激进实盘 + 自建模拟盘
- 核心模块：`portfolio/`（实盘选股+下单）、`simulation/`（模拟撮合常驻进程）、`factors/`、`sentiment/`、`ml/`
- 部署 Python 3.9.6（README 声明 3.9+），cron 在 15:30 触发 `run_daily.sh`
- 最近改动主线：模拟盘决策理由通俗化、调仓换股、持仓维度分析、AI 解读
- 工作区 clean，所有问题来自现网代码静态/动态审查

---

## Phase 1 — 阻断性问题（不修则模拟盘完全不可用）

### Fix 1.1：补回缺失的 `simulation/trade_log.py` 与 `simulation/__init__.py`

**症状**：
```
$ python3 -c "from simulation.engine import SimEngine; SimEngine()"
ModuleNotFoundError: No module named 'simulation.trade_log'
```

**调查结论**：
- commit `b6ae3c6` 的 message 声称添加了 `simulation/trade_log.py`，但 `git log --diff-filter=A -- simulation/trade_log.py` 找不到该文件被 add 的记录。`.gitignore` 也未匹配。漏 add。
- `simulation/__init__.py` 同样缺失。

**调用方期望签名（按 `engine.py` / `report.py` 当前 import 反推）**：
```python
# simulation/trade_log.py
def _get_conn() -> sqlite3.Connection: ...
def load_sim_portfolio() -> dict: ...                                # {"cash": float, "holdings": {code: {shares, avg_cost, buy_date}}}
def save_sim_portfolio(portfolio: dict) -> None: ...
def save_order(order) -> None: ...                                   # 写 sim_orders 表
def update_order_status(order) -> None: ...                          # 更新订单状态
def save_trade(symbol, name, side, shares, price, amount, fee, *,
               profit: float = 0.0, reason: str = "",
               order_id: int = 0) -> None: ...                       # 写 sim_trades 表
def save_snapshot(*, cash, market_value, total_value,
                  daily_return, total_return, positions, trades) -> None: ...
def get_latest_snapshot() -> dict | None: ...
def get_today_trades() -> list[dict]: ...
def get_trades(start_date: str = None, end_date: str = None) -> list[dict]: ...
def get_snapshots(limit: int = 30) -> list[dict]: ...                # 按日期降序
```

**实现要求**：
- SQLite 库路径：`config.settings.SIM_DB_PATH`（参考 PROGRESS.md 2026-04-17 段）
- 持仓 JSON：`data/sim_portfolio.json`
- 三张表：`sim_orders / sim_trades / sim_snapshots`，trades 行至少含 `id, date, symbol, name, side, shares, price, amount, fee, profit, reason, order_id`
- snapshot 行含 `date, cash, market_value, total_value, daily_return, total_return, positions(JSON), trades(JSON)`
- 所有连接用 `with` 语句管理生命周期，避免异常泄漏

**验收**：
```bash
python3 -c "from simulation.engine import SimEngine; e = SimEngine(); print(e.status())"
python3 -m pytest tests/ -v   # 不应有 ImportError
```

---

### Fix 1.2：`simulation/matcher.py:153` PEP 604 联合类型在 Python 3.9 报错

```python
# 现状
def check_stop_loss(...) -> Order | None:
# TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
```

**修复**（任选其一，推荐方案 A）：
```python
# A. 文件顶部加（推荐，最小改动）
from __future__ import annotations

# B. 改类型注解
from typing import Optional
def check_stop_loss(...) -> Optional[Order]:
```

**验收**：`python3 -c "import simulation.matcher"` 无报错。

---

### Fix 1.3：涨跌停判断永远不触发（`simulation/matcher.py:98, 127`）

**症状**：`fetch_quotes_batch` 在 `simulation/matcher.py:259-262` 把 `bid1/ask1` 直接写为当前价，导致 `_match_buy` 中 `if ask1 <= 0` 永远 False，涨停日仍以 `price + 0.01` 模拟成交。

**修复方向**：
1. 在 `fetch_quotes_batch` 产出 `is_limit_up / is_limit_down` 布尔字段，判定逻辑：
   ```python
   limit_pct = 0.10  # 主板/创业板 10%；688/科创 20%；8xx/4xx 北交所 30%
   if code.startswith(("688", "8", "4")):
       limit_pct = 0.20 if code.startswith("688") else 0.30
   is_limit_up = prev_close > 0 and price >= prev_close * (1 + limit_pct - 0.001)
   is_limit_down = prev_close > 0 and price <= prev_close * (1 - limit_pct + 0.001)
   ```
   （留 0.1% 容差应对四舍五入）
2. `_match_buy` 改用 `if quote.get("is_limit_up"): order.status = "rejected"; ...`
3. `_match_sell` 同理判 `is_limit_down`
4. 删除现 L98-107 / L127-135 永远进不去的备援分支

**验收**：
- 单测：构造 `prev_close=10.0, price=11.0` 的 quote，买入订单应 `rejected`，reason 含 `[涨停]`
- 同理跌停场景 → 卖单 rejected
- 主板 10%、创业 20%、北交所 30% 各覆盖一个用例

---

### Fix 1.4：`portfolio/allocator.py:858-867` `_simulate_execution` 参数错误

**现状**：
```python
def _simulate_execution(tracker, actions: list, alloc: dict):
    for a in actions:
        if a["action"] == "买入" and a["code"] != "当前持仓":
            tracker.update_after_buy(
                a["code"],
                shares=1,         # ← 写死 1
                price=a["amount"],# ← 把"金额"当"价格"
                cost=0,
            )
```

跑 `python main.py deploy --simulate` 一次就会污染 `data/portfolio.json`。

**修复**：标准 deploy 模式（ETF + 个股）已被激进 `run_live_deploy` 替代，且当前 `actions` 列表里没有 `shares`/`price` 字段。直接：
1. **删除** `_simulate_execution` 函数
2. **删除** `run_deploy` 中调用它的分支（`allocator.py:841-843`）
3. **保留**注释或返回提示："standard deploy --simulate 已弃用，请使用 `live --simulate`"

**验收**：`grep -rn "_simulate_execution\b" .` 只在删除位置看到 0 处引用。

---

## Phase 2 — 语义错误 / 死代码

### Fix 2.1：持仓时长改用交易日（影响 5 处）

**现状**：所有 `(today - buy_dt).days` 都是日历日，但策略阈值（`MAX_HOLDING_DAYS=20`）按交易日设计 — 跨周末会提前触发超时调仓。

**统一**：在 `simulation/matcher.py` 已有的 `_calc_days` 基础上，新增 `_calc_trade_days(buy_date) -> int`，用 `chinese_calendar.get_workdays(buy_dt, today)` 计数（已是项目依赖）。

**所有调用点替换**：
- `portfolio/allocator.py:255-257`
- `simulation/matcher.py:170, 172, 176`（也包括 reason 文本）
- `simulation/engine.py:582-587, 631-635, 755-761`

**注意**：reason 字符串里的"持有 N 日"也要改为"持有 N 交易日"，便于人类阅读。

**验收**：用 `2026-04-04`（周五）为 `buy_date`，`today=2026-04-08`（周二），应返回 2 个交易日（中间周末跳过）。

---

### Fix 2.2：删除死代码

| 文件:行 | 函数 | 处置 |
|---------|------|------|
| `portfolio/allocator.py:701-718` | `_simulate_execution_live` | 删除（无调用方） |
| `simulation/matcher.py:196-241` | `fetch_quote_with_depth` | 删除（无调用方且实现未完成） |

**注意**：删除后再 `grep -rn` 确认无引用残留。

---

### Fix 2.3：重构 `humanize_reason` — 用结构化 dict 替代正则反解析

**现状**：
- `portfolio/trade_utils.py:22` 与 `simulation/report.py:37` 各有一份"用正则解析自家拼出来的字符串"的实现
- 两份已分叉：trade_utils 含「调仓换股」处理、report 含 `mom_5d` 处理
- 数据源 `get_stock_picks_live`（`allocator.py:483-497`）已经把因子值放在 dict 里，却又把它们 join 成字符串再让下游用正则拆解

**修复**：
1. 在 `portfolio/allocator.py:get_stock_picks_live` 的 pick dict 里增加结构化字段：
   ```python
   picks.append({
       ..., "reason": reason,
       "reason_data": {                # ← 新增
           "factor_rank": factor_rank,
           "ml_rank": ml_rank,
           "in_both": bool(row["in_both"]),
           "key_factors": {"mom_20d": ..., "pe_ttm": ..., "pb": ..., ...},
           "predicted_return": pred_ret,  # 可为 None
       },
   })
   ```
2. 把 `simulation/engine._generate_next_plan` 把 `reason_data` 也带进 `plan["buys"][*]`，并在 `_execute_order → save_trade` 时一并写入 `sim_trades` 新增列 `reason_data` (JSON)
3. 新建 `portfolio/reason_text.py`，集中放 `humanize_reason(reason_data: dict, name: str) -> str`：纯字典输入，无正则
4. `trade_utils.py` 与 `report.py` 都改为调用这个集中函数；删除两份正则版本
5. trade_log schema 升级时，老数据（无 `reason_data`）要 graceful fallback 到原 `reason` 字符串（保留旧正则解析作为兜底，但加 `# legacy` 注释）

**验收**：
- `grep -rn "re\.search.*因子#" .` 应只剩 1 处（legacy fallback）
- 终端 / 微信 / 模拟盘日报三处理由文案完全一致
- `portfolio.allocator` 测试：传入 dict → 输出预期文案

---

### Fix 2.4：`factors/calculator.py` `logger` 移到顶部

**现状**：`logger = logging.getLogger(__name__)` 在文件 322 行（末尾），但 L157 已经使用。靠 import 顺序碰巧能跑。

**修复**：把 L322-323 的两行：
```python
import logging
logger = logging.getLogger(__name__)
```
移到文件顶部 import 区，与其他 import 同列。删除末尾两行。

---

## Phase 3 — 一致性与健壮性

### Fix 3.1：抽取 `_calc_days` 重复（与 Fix 2.1 合并完成）

完成 Fix 2.1 后，`engine.py` 中 4 处独立的 `days_held = (datetime.now() - buy_dt).days` 都应改为调用 `matcher._calc_trade_days(buy_date)`。

---

### Fix 3.2：`5000` magic number 提到 settings

新增到 `config/settings.py`：
```python
MIN_BUY_CAPITAL = 5000  # 单次买入触发的最低可用资金
```

替换：
- `portfolio/allocator.py:603, 775`
- `simulation/engine.py:820, 858, 859`

---

### Fix 3.3：`simulation/engine.py` 函数内重复 import `fetch_quotes_batch`

把模块顶部加 `from simulation.matcher import fetch_quotes_batch`，删除 L348/676/743/849/864 五处函数内 import。

---

### Fix 3.4：`engine.py:786` 条件冗余

```python
# 现状
if remaining_holdings and rotation_done < max_rotation:
# 修复（rotation_done 此处恒为 0）
if remaining_holdings:
```

---

### Fix 3.5：`report.py:487` truthy 误判 profit

```python
# 现状
profit_str = f"{t['profit']:+,.0f}元" if t.get("profit") else ""
# 修复
profit_str = f"{t['profit']:+,.0f}元" if t.get("profit") is not None else ""
```

---

### Fix 3.6：`report.py:139-143` 死代码

`_humanize_reason` 中读取 `trade["dimension_scores"]` 的分支永远进不去（`save_trade` 没传该字段）。
- 选项 A：在 Fix 2.3 重构时把 `dimension_scores` 一并写进 trade（推荐）
- 选项 B：删除 L139-143 这段无效代码

---

### Fix 3.7：`engine.py:218` reset 类型不一致

```python
# 现状
self.daily_plan = []
# 修复（与其他地方读写保持 dict 一致）
self.daily_plan = {}
```

---

### Fix 3.8：`calculator.py` `calc_volume_price` 不对称 fallback

`prior_ret` 在 `len(df) < 20` 时退回 `0`，但 `prior_vol` 退回到全样本均值，两端语义错位。统一：要么都用全样本，要么都返回 NaN。建议都返回 NaN（因子缺失下游会自然处理）。

---

### Fix 3.9：合并 `_get_decision_note` 与 `_get_holding_analysis`

两者读同一个 `sim_daily_plan.json`，合并为一个 helper：
```python
def _load_daily_plan() -> dict:
    """读一次 plan，所有字段都从这里取"""
    ...
```

每次 `daily_report` 只 IO 一次。

---

### Fix 3.10：`engine.py:24` 移除模块级 sys.path

```python
# 现状（污染调用方）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 修复：删掉。仅在 main.py 入口处（如有需要）处理。
```

---

## 整体验收清单

完成所有 Phase 后：

- [ ] `python3 -c "from simulation.engine import SimEngine; SimEngine()"` 无报错
- [ ] `python3 -m pytest tests/ -v` 全过（57 个或新增）
- [ ] `python3 main.py sim --run-once` 正常出日报
- [ ] `python3 main.py live` 正常出操作清单
- [ ] `python3 scripts/preflight.py` 全过
- [ ] `grep -rn "_simulate_execution\b\|fetch_quote_with_depth\b" .` 仅在历史文档中出现
- [ ] `grep -rn "5000" portfolio/ simulation/` 无残留 magic number
- [ ] `grep -rn "re\.search.*因子#" .` ≤ 1 处（仅 legacy fallback）
- [ ] PROGRESS.md 追加一节记录修复内容（按现有日期倒序格式）

---

## 提交规范

按现有 commit 风格（中文 + 简短说明），建议拆成 3 个 commit 对应 3 个 Phase：

```
fix(simulation): 补回 trade_log + 修复 Python 3.9 兼容 + 涨跌停撮合
fix: 持仓时长改交易日 + humanize_reason 重构为结构化 dict + 清理死代码
chore: 抽常量 + 统一 import + 修正若干一致性问题
```

每个 commit 末尾保留：
```
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```
