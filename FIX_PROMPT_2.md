# 代码修复 Prompt v2 — 第二轮 review 发现的新问题

> 上一轮（FIX_PROMPT.md）的 Phase 1/2/3 已被 commit 234c42d / 35cb34a / 85b6895 执行完成，57 个测试全过。  
> 本轮针对**修复执行过程中新引入的 bug** 和 **修复链断裂**，按严重性排序。  
> 全部修改集中在 4 个文件，建议一个 commit 完成。

---

## 项目背景（精简）

- A 股量化系统，主战场含主板（60xxx / 00xxx）+ 创业板（300xxx）
- 模拟盘三模块：`simulation/{engine,matcher,trade_log,report}.py`，结构化理由从 `portfolio/reason_text.py` 集中产出
- Python 3.9.6，`pytest tests/ -v` 当前 57 项全过
- 工作区 clean，本轮所有问题均通过静态阅读 + 实际 `_check_limit` 调用验证得出

---

## NB1（Critical）：创业板 300xxx 涨跌停限制错误

**文件**：`simulation/matcher.py:68-73`

**现状**：
```python
def _check_limit(code: str, price: float, prev_close: float) -> tuple:
    ...
    if code.startswith("688"):
        limit_pct = 0.20
    elif code.startswith(("8", "4")):
        limit_pct = 0.30
    else:
        limit_pct = 0.10        # ← 创业板 300xxx 也走这里，但实际限制是 20%
```

**实测复现**：
```python
>>> from simulation.matcher import _check_limit
>>> _check_limit("300001", 11.0, 10.0)   # 涨 10%（创业板远未涨停）
(True, False)                             # ← 错！应为 (False, False)
>>> _check_limit("300001", 11.5, 10.0)   # 涨 15%（仍未涨停）
(True, False)                             # ← 错
>>> _check_limit("300001", 12.0, 10.0)   # 涨 20%（应该涨停）
(True, False)                             # 这个对
```

**后果**：所有创业板股票（系统主战场之一，`portfolio/trade_utils.py:36` 的 `_TRADEABLE_RE` 显式包含 300）只要涨幅落在 9.9%~19.9% 区间就被错误判涨停，撮合器拒绝买入；同样跌停场景拒绝卖出。模拟盘从此对 ~半数标的池基本无法成交。

**修复**：把 300 与 688 合并成 20% 分支
```python
if code.startswith("688") or code.startswith("300"):  # 科创板 + 创业板，限制 20%
    limit_pct = 0.20
elif code.startswith(("8", "4")):                     # 北交所 30%
    limit_pct = 0.30
else:                                                  # 沪深主板 10%
    limit_pct = 0.10
```

**验收**（加到 `tests/`，建议新建 `tests/test_matcher.py`）：
```python
def test_check_limit_chuangye():
    from simulation.matcher import _check_limit
    # 创业板未涨停
    assert _check_limit("300001", 11.0, 10.0) == (False, False)
    assert _check_limit("300001", 11.5, 10.0) == (False, False)
    # 创业板涨停（+20%，留 0.1% 容差所以 +19.9% 也算）
    assert _check_limit("300001", 12.0, 10.0)[0] is True
    # 主板 +10% 涨停
    assert _check_limit("600519", 11.0, 10.0)[0] is True
    # 科创板 +20% 涨停
    assert _check_limit("688001", 12.0, 10.0)[0] is True
    # 跌停对称
    assert _check_limit("300001", 8.0, 10.0)[1] is True
```

---

## NB2（Critical）：`reason_data` 数据链在 engine 断裂

**文件**：`simulation/engine.py:820-827`

**现状**：
```python
for p in picks:
    plan["buys"].append({
        "code": p["code"],
        "name": p.get("name", ""),
        "shares": p["shares"],
        "price": p["price"],
        "amount": p["amount"],
        "reason": p.get("reason", ""),
        # ← 缺 reason_data
    })
```

**链路追踪**：
```
allocator.get_stock_picks_live → picks 含 reason_data ✓ (Fix 2.3)
   ↓
engine._generate_next_plan → plan["buys"] 丢弃 reason_data ✗ ← 断点
   ↓
engine._get_order_reason_data 从 plan["buys"] 查 → 永远拿到 None
   ↓
engine._execute_order → save_trade(reason_data="") → DB 列永远空串
   ↓
report._humanize_reason → trade["reason_data"] 空 → 永远走 legacy 正则
```

**后果**：整个 Fix 2.3 重构（让模拟盘日报用结构化 dict 翻译理由）在数据流第一站就丢了，永远走 legacy fallback。日报里看到的依然是用旧正则解出来的文案。

**修复**：补一行
```python
for p in picks:
    plan["buys"].append({
        "code": p["code"],
        "name": p.get("name", ""),
        "shares": p["shares"],
        "price": p["price"],
        "amount": p["amount"],
        "reason": p.get("reason", ""),
        "reason_data": p.get("reason_data"),   # ← 新增
    })
```

**验收**：
```python
# tests/test_engine.py 新增
def test_plan_carries_reason_data(tmp_path, monkeypatch):
    """模拟一次 _generate_next_plan，验证 plan['buys'] 含 reason_data"""
    # （需要 mock get_stock_picks_live 返回带 reason_data 的 picks）
    ...
```
或者最简：跑一次完整 `python3 main.py sim --run-once`，去 SQLite `sim_trades` 表查 `reason_data` 列应非空：
```bash
sqlite3 data/sim_trading.db "SELECT reason_data FROM sim_trades WHERE side='buy' ORDER BY id DESC LIMIT 1"
```

---

## NB3（Critical）：`portfolio/trade_utils.py` 4 处调用 humanize_reason 没传 reason_data

**文件**：`portfolio/trade_utils.py:112, 126, 152, 162`

**现状**：
```python
# L112, 126, 152, 162 — 全部只传两个参数
reason_str = humanize_reason(a.get('reason', ''), a.get('name', ''))
```

`a` 来自 `buy_actions / sell_actions`。`buy_actions` 由 `get_stock_picks_live` 产出，已带 `reason_data`，但调用时丢弃。

**后果**：实盘 `python main.py live --push` 走的就是 `trade_utils.format_checklist` / `format_push_message` 这两个函数。用户每天微信收到的清单中买入理由 — **同样永远走 legacy fallback**，与模拟盘问题对称。

**修复**：4 处全加 `reason_data=a.get('reason_data')`
```python
# L112 (format_checklist 卖出)
reason_str = humanize_reason(
    a.get('reason', ''), a.get('name', ''),
    reason_data=a.get('reason_data'),
)

# L126 (format_checklist 买入) — 注意现在是 inline 字符串
lines.append(
    f"     {humanize_reason(a['reason'], a.get('name', ''), reason_data=a.get('reason_data'))}"
)

# L152, L162 (format_push_message 同样)
reason_str = humanize_reason(
    a.get('reason', ''), a.get('name', ''),
    reason_data=a.get('reason_data'),
)
```

**注意**：sell_actions（`check_holdings` 产出）目前没有 reason_data，传 None 走 legacy fallback 是预期行为，无需补 reason_data。但调用签名要保持一致性，4 处都加。

**验收**：单测构造一个含 `reason_data` 的 buy_action，调 `format_checklist` / `format_push_message`，检查输出含结构化版翻译（例如"短期强势(20日涨XX%)"）而非 legacy fallback 的字面。

---

## M1（Medium）：`simulation/trade_log.py` 默认资金硬编码 20000

**文件**：`simulation/trade_log.py:82, 87`

**现状**：
```python
def load_sim_portfolio() -> dict:
    if not os.path.exists(_PORTFOLIO_PATH):
        return {"cash": 20000.0, "holdings": {}}
    try:
        with open(_PORTFOLIO_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"cash": 20000.0, "holdings": {}}
```

`config/settings.py` 已有 `INITIAL_CAPITAL = 20000`。如果用户改 settings 加资金，模拟盘起始仍是 20000，与实盘脱钩。

**修复**：
```python
from config.settings import INITIAL_CAPITAL

def load_sim_portfolio() -> dict:
    if not os.path.exists(_PORTFOLIO_PATH):
        return {"cash": float(INITIAL_CAPITAL), "holdings": {}}
    try:
        with open(_PORTFOLIO_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"cash": float(INITIAL_CAPITAL), "holdings": {}}
```

---

## M2（Medium）：`sim_trades.reason_data` 默认值与其他 JSON 列不一致

**文件**：`simulation/trade_log.py:62`

**现状**：
```sql
positions TEXT DEFAULT '{}',
trades TEXT DEFAULT '[]',
...
reason_data TEXT DEFAULT ''      ← 不一致
```

`report.py:67` 在 reason_data 是字符串时调 `json.loads(reason_data)`，遇到 `''` 会 `JSONDecodeError`。当前被 try/except 兜住，但语义混乱。

**修复（任选）**：

A. 改默认值（需考虑迁移，对老库无作用）：
```sql
reason_data TEXT DEFAULT '{}'
```

B. 在 `save_trade` 时显式 json 化：
```python
def save_trade(..., reason_data: str = "") -> None:
    # 接受 dict 或 str，统一成 JSON 字符串
    if isinstance(reason_data, dict):
        reason_data = json.dumps(reason_data, ensure_ascii=False)
    elif not reason_data:
        reason_data = "{}"
    ...
```

推荐 B（向后兼容老调用方），并把 `engine._get_order_reason_data` 改为返回 dict 而非 JSON 字符串，让 trade_log 唯一负责序列化。

---

## 提交规范

建议一个 commit 收尾本轮：

```
fix: 创业板涨跌停限制 + reason_data 数据链贯通 + 模拟盘默认资金读 settings

NB1: 创业板 300xxx 加入 20% 涨跌停限制（之前误判为 10%）
NB2: engine._generate_next_plan 把 picks.reason_data 拷贝进 plan["buys"]
NB3: trade_utils 4 处 humanize_reason 调用补 reason_data 参数
M1:  trade_log 默认资金从 settings.INITIAL_CAPITAL 读取
M2:  reason_data 序列化集中到 save_trade，避免 JSON 默认值不一致

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 整体验收清单

完成本轮后：

- [ ] `python3 -m pytest tests/ -v` 仍 57+ 项全过
- [ ] 新增 `tests/test_matcher.py` 至少覆盖创业板/主板/科创板/北交所 4 板的 _check_limit
- [ ] `python3 main.py sim --run-once` 跑通后，`sqlite3 data/sim_trading.db "SELECT reason_data FROM sim_trades ORDER BY id DESC LIMIT 1"` 应输出 JSON 而非空串
- [ ] `python3 main.py live` 输出的买入清单中，理由文案使用结构化版（如"短期强势(20日涨XX%)"），不再含 `因子#N` 这类原始 reason 字符串残留
- [ ] `grep -rn "20000\.0\|20000\b" simulation/` 无硬编码资金
- [ ] PROGRESS.md 追加一节记录本轮修复
