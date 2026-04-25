# 代码修复 Prompt v3 — 第三轮 review（扩展模块）

> FIX_PROMPT.md / FIX_PROMPT_2.md 的修复都已合并（commit 234c42d / 35cb34a / 85b6895 / 365401c），62 项测试全过。  
> 本轮把 review 范围扩展到之前没覆盖的模块：`server.py` / `data/` / `ml/` / `scripts/` / `backtest/` / `factors/data_loader.py` / `strategy/` / `alert/`，发现的问题按严重性排序如下。  
> 全部改动建议拆 2 个 commit：H 一个、M+L 一个。

---

## 项目背景（精简）

- 主入口 `main.py` 把 cron / 手动 / Web 三条触发链都汇到 `portfolio/allocator.py`
- Web 持仓同步服务 `server.py` 默认绑 `0.0.0.0`，DEPLOY.md 建议外接 nginx
- 测试：`pytest tests/ -v` 当前 62 项全过，本轮新增 fix 时不应破坏
- 工作区 clean，每条问题都注明 file:line 与建议修复方向

---

## H1（High）：`server.py` 安全 + 鲁棒性

整个 server.py 都需要一轮硬化。优先级 a → e。

### a) 默认 `WEB_TOKEN = "pj_quant_2026"` 弱口令上线（server.py:28）

```python
WEB_TOKEN = os.getenv("WEB_TOKEN", "pj_quant_2026")
```
- 可猜测、字典内、`tests/test_server.py` 也以此为默认 → 暗示线上可能就用这个
- DEPLOY.md "云主机部署" 明确说要 `nohup python3 server.py` 后台跑，没强制 nginx + IP 白名单的情况下，攻击者凭 `?token=pj_quant_2026` 可全权管理持仓

**修复**：缺失 env 时启动报错，不允许默认值
```python
WEB_TOKEN = os.environ.get("WEB_TOKEN")
if not WEB_TOKEN:
    raise RuntimeError(
        "WEB_TOKEN env 未设置。请生成强随机 token 后通过环境变量传入：\n"
        "  WEB_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') python3 server.py"
    )
```
`tests/test_server.py` 改为在 fixture 里 `monkeypatch.setenv("WEB_TOKEN", "test-token-only")` 而不是依赖默认值。

### b) `/api/sync` 没幂等性（server.py:170-216）

```python
warning = None
if os.path.exists(sync_path):
    warning = "今日已有同步记录，本次为追加操作"
# 然后照常 update_after_buy / update_after_sell
```
用户手机上重复点"确认同步" = 重复扣款。

**修复**（最小方案）：
1. `SyncBody` 加 `client_request_id: str` 字段
2. 在 `logs/sync/{date}.json` 里维护已处理的 `request_id` 集合
3. 同 `request_id` 第二次到达直接返回上次结果，不重复执行
4. 前端 `doSync()` 用 `crypto.randomUUID()` 生成

或者更简单的：请求体序列化指纹（`hashlib.sha256(canonical_json(body))`）作为唯一键。

### c) HTML 模板未转义（server.py:255-279）

`signals.get("buy_signals", [])[*].reason` 由 LLM `humanize_reason` 输出，理论受控但**不可信**。一旦 reason 含 `<script>` 或 `"` 直接破坏 HTML 结构 / 触发 XSS。

**修复**：
```python
import html
# 渲染时所有动态字段都过 html.escape
buy_checks += f"""
<label><input type="checkbox" data-action="buy" data-code="{html.escape(b['code'])}" 
       data-shares="{shares_val}" data-price="{price_val}"> 
买入 {html.escape(b['code'])} {shares_val}股@{price_val:.2f} ({html.escape(b.get('reason', ''))})</label><br>"""
```
所有 `code` / `name` / `reason` / `signal["reason"]` 都要过。

### d) 持仓写入无并发保护

`PortfolioTracker.update_after_*` → `data.storage.save_portfolio` → `pd.DataFrame([...]).to_sql("portfolio", if_exists="replace")` 是非原子的 read-modify-write，多客户端同时同步会丢操作。

**修复（最小方案）**：用文件锁
```python
# data/storage.py
import fcntl
_LOCK_PATH = os.path.join(os.path.dirname(DB_PATH), ".portfolio.lock")

def save_portfolio(state: dict):
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    with open(_LOCK_PATH, "w") as lock_fp:
        fcntl.flock(lock_fp, fcntl.LOCK_EX)
        try:
            # 原有逻辑
            ...
        finally:
            fcntl.flock(lock_fp, fcntl.LOCK_UN)
```

### e) 卖出强制全仓（server.py:179-181）

```python
shares = tracker.holdings.get(item.code, {}).get("shares", 0)
ok = tracker.update_after_sell(item.code, item.price, cost)
```
`SellItem` 没暴露 `shares` 字段、`update_after_sell` 也只接受 code+price → 不支持部分减仓。

**修复**：
1. `SellItem` 加可选 `shares: Optional[int] = None`
2. `update_after_sell` 接受 `shares` 参数，None 时全卖；非 None 时部分卖（更新 shares 和 cash，保留 holding 记录）
3. 前端 HTML 加 shares 输入

不打算上部分减仓功能也行，但需在 API 文档明确"卖出 = 清仓"。

**验收**：
- 修复后启动 `python3 server.py` 不传 WEB_TOKEN 应报错
- 同 request_id 重复调用 `/api/sync` 第二次返回 `{"idempotent": true, "previous_result": ...}`
- `tests/test_server.py` 全过
- 渲染含 `<script>` 的 fake reason，输出 HTML 中应是 `&lt;script&gt;`

---

## H2（High）：残留 magic number `5000`

Phase 3 抽出 `MIN_BUY_CAPITAL` 时漏了两处。

| 文件:行 | 现状 |
|--------|------|
| `scripts/postflight.py:106` | `if slots > 0 and available_cash >= 5000:` |
| `server.py:231` | `if tracker.cash < 5000:` |

**修复**：两处都 `from config.settings import MIN_BUY_CAPITAL` 后替换 `5000` → `MIN_BUY_CAPITAL`，并 grep 复查全 repo 无残留。

---

## H3（High）：`data/storage.py:119` 残留硬编码 20000

```python
def load_portfolio() -> dict:
    ...
    return {"cash": 20000.0, "holdings": {}, "total_value": 20000.0}
```

`trade_log.py` 已修过同类问题，但实盘 `tracker.PortfolioTracker` 走的是 `data/storage.load_portfolio` —— 这边没改。用户改 `INITIAL_CAPITAL=50000` 后，初次实盘启动持仓仍按 20000 算。

**修复**：
```python
from config.settings import INITIAL_CAPITAL

def load_portfolio() -> dict:
    ...
    return {
        "cash": float(INITIAL_CAPITAL),
        "holdings": {},
        "total_value": float(INITIAL_CAPITAL),
    }
```

---

## M1（Medium）：`backtest/engine.py` NAV 曲线稀疏 + Sharpe 失真

**文件**：`backtest/engine.py:78-145, 200-205`

**症状**：
```python
for _, signal in signals.iterrows():     # ← 只遍历 signal 行
    ...
    nav_records.append({"date": date, "nav": ...})  # ← 只有 signal 日期才有 nav 数据点
```

由此衍生：
- L196-198 max_drawdown 基于稀疏 nav，跨多日的回撤会被合并成一根
- L202-205 sharpe `daily_returns = nav_df["nav"].pct_change()` 把"signal 间隔的收益"当成"日收益"，再 ×√252 年化 → 数值失真
- L191-193 annualize 用日历日 `(date_end - date_start).days` 是对的，但分子 `(1+total_return)^(1/years)-1` 已经准确，问题只在 std

**修复**：把 nav 计算从"按 signal"改成"按交易日"
```python
all_dates = sorted(set(d for df in price_data.values() for d in df["date"]))
sig_iter = iter(signals.sort_values("date").to_dict("records"))
pending = next(sig_iter, None)

for d in all_dates:
    # 处理这一天的信号（可能多条）
    while pending and pending["date"] <= d:
        # 执行 buy/sell, 更新 holdings/cash/trades
        pending = next(sig_iter, None)
    # 记录每日 nav
    holdings_value = sum(...日内最新收盘价...)
    nav_records.append({"date": d, "nav": cash + holdings_value, ...})
```

---

## M2（Medium）：`backtest/engine.py:113` 不支持加仓

```python
elif action == "buy" and symbol and symbol not in holdings:
```

小盘多因子月度调仓策略本来支持持仓覆盖（`PortfolioTracker.update_after_buy` 有加仓均价合并逻辑）。回测引擎"已持有就跳过" → **回测结果与实盘策略不一致**。

**修复**：去掉 `symbol not in holdings` 限制，按现有 holdings 调用与 `update_after_buy` 一样的均价合并逻辑：
```python
elif action == "buy" and symbol:
    buy_amount = cash - 100
    if buy_amount > price * 100:
        shares = int(buy_amount / price / 100) * 100
        ...
        if symbol in holdings:
            old = holdings[symbol]
            total_shares = old["shares"] + shares
            total_cost = old["avg_cost"] * old["shares"] + price * shares
            holdings[symbol] = {
                "shares": total_shares,
                "avg_cost": total_cost / total_shares,
            }
        else:
            holdings[symbol] = {"shares": shares, "avg_cost": price}
```

---

## M3（Medium）：`preflight.py:23` `_last_trade_date` 不认识法定节假日

**现状**：
```python
def _last_trade_date() -> str:
    today = datetime.now()
    if today.weekday() == 0:    # 周一 → 上周五
    elif today.weekday() >= 5:  # 周末 → 周五
    else:                        # 其他 → 昨天
```

**问题**：节后第一个工作日（如初八开盘），"昨天"是除夕（非交易日），`_last_trade_date()` 返回除夕日期 → 数据新鲜度检查必然失败 → preflight 误报。

**修复**（`chinese_calendar` 是项目已有依赖）：
```python
import chinese_calendar

def _last_trade_date() -> str:
    d = datetime.now().date() - timedelta(days=1)
    # 倒推到最近一个交易日
    while not chinese_calendar.is_workday(d) or d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")
```

---

## M4（Medium）：`alert/notify.py:74-116` 旧 ETF 推送格式与新格式分叉

`format_signal_message` / `format_no_signal_message` 是 ETF 时代旧推送模板，仍被 `alert/daily_runner.py` 调用（`python main.py signal --push` 命令链）。当前实盘走的是 `portfolio/trade_utils.format_push_message` 结构化版。

两份模板分叉、不会被未来的 `humanize_reason` 改造覆盖。

**处置**（任选）：
- A. 明确 "ETF 单独路径" — 在 docstring 标注，加 `# ETF only` 注释
- B. 删除 `signal` 命令、统一到 `live --push`（README 命令表已经把 `live` 标主推荐）
- C. 把 ETF 推送也接到 `format_push_message`（最优但工作量最大）

推荐 B：`alert/daily_runner.py` 整个删，`main.py:336-338` 删 signal 分支，notify.py 只保留 `send_message` / `send_to_all`。

---

## M5（Medium）：`backtest/engine.py:202` 与 `simulation/report.py:697` Sharpe 公式分叉

- Backtest: `(returns.mean() - 0.02/252) / std * √252`（含 2% 无风险利率）
- Simulation: `returns.mean() / std * √252`（无风险利率 0）

两个绩效报告口径不同，对比看会误导。

**修复**：抽公共函数 `metrics/sharpe.py`（或 `analytics/perf.py`）：
```python
def sharpe_ratio(daily_returns: np.ndarray, risk_free: float = 0.02) -> float:
    """日频收益序列 → 年化夏普"""
    if len(daily_returns) < 2 or daily_returns.std() == 0:
        return 0.0
    excess = daily_returns - risk_free / 252
    return float(excess.mean() / daily_returns.std() * np.sqrt(252))
```
backtest / simulation 两端都调它，传同一 `risk_free` 默认值。

---

## M6（Medium）：`factors/data_loader.py:50-65` 逐表 SQL 性能

```python
for code in cached:
    table = _safe_table_name(code)
    row = conn.execute(
        f"SELECT total_mv FROM {table} WHERE total_mv IS NOT NULL ORDER BY date DESC LIMIT 1"
    ).fetchone()
```
4400+ 只股票 = 4400 次 SQL 往返。SQLite 即便是本地，单次 `fetchone` 也有 ~0.5ms 开销，总体 2-3 秒空跑成本。

**优化**：
- A. 维护一张汇总表 `latest_market_cap (code, total_mv, updated_at)`，每次 tushare_fundamentals 增量后刷新
- B. 用 `UNION ALL` 拼一条大 SQL（生成 4400 个 subquery 拼接，看起来丑但 SQLite 处理得动）

A 更优雅。等到 tushare 增量结束时同时更新汇总表。

---

## M7（Medium）：`ml/ranker.py:27, 39, 41, 43` 模型路径相对

```python
MODEL_DIR = "ml/models"
IMPORTANCE_PATH = os.path.join(MODEL_DIR, "feature_importance.json")
HISTORY_PATH = os.path.join(MODEL_DIR, "model_history.json")
PRODUCTION_MODEL = os.path.join(MODEL_DIR, "xgb_ranker.json")
```

从其他目录执行（如 cron 用 `cd /tmp && python3 /path/to/main.py train`）会在 cwd 下创建空 `ml/models/`，原模型读不到。

**修复**：和 `scripts/preflight.py` 一致：
```python
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODEL_DIR = os.path.join(_PROJECT_ROOT, "ml", "models")
...
```

---

## L 系列（Low / 风格）

- **L1. `tests/test_allocator.py:88, 102`** 用 `timedelta(days=35)` 估算 25 交易日，跨长假会偶发失败。建议直接 `monkeypatch.setattr("simulation.matcher._calc_trade_days", lambda _: 25)`。
- **L2. `strategy/small_cap.py:8` docstring 错误**：写 "个股止损 -10%"，实际 settings 是 -8%。改注释或删除（`SmallCapStrategy.stop_loss` 默认值仅是占位，实际由 `STOP_LOSS_PCT` 控制）。
- **L3. `portfolio/trade_utils.py:11-12` 注释含糊**：现状是仅交易主板+创业板，对科创板/北交所只识别行情；建议明确写出。
- **L4. `factors/calculator.py:26, 38, 50` mutable default args**：list 默认参数 — Python 反模式，未实际修改不出 bug，但建议改 `None` + 函数体内默认。
- **L5. `scripts/preflight.py:43, 132` `random.sample` 无种子**：每次抽不同样本失败难复现。在 cron 场景下，加 `random.seed(datetime.now().strftime("%Y%m%d"))` 让同日复现。

---

## 提交建议

拆 2 个 commit：

```
fix(security): server.py 强制 WEB_TOKEN + 幂等性 + HTML 转义 + 并发锁
- a) WEB_TOKEN 缺失时启动报错，不再用 pj_quant_2026 作默认
- b) /api/sync 加 client_request_id 幂等性
- c) HTML 模板所有动态字段过 html.escape
- d) save_portfolio 用 fcntl 文件锁
- e) SellItem 支持可选 shares 部分减仓
+ tests/test_server.py 用 monkeypatch.setenv 而非默认值

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

```
fix: 抹平残留 5000/20000 硬编码 + 回测引擎按日 NAV + 节假日感知 + 合并 Sharpe 公式
- H2: scripts/postflight.py + server.py 的 5000 → MIN_BUY_CAPITAL
- H3: data/storage.py:119 默认资金读 INITIAL_CAPITAL
- M1: backtest/engine.py NAV 改为按交易日记录（修复稀疏曲线 + Sharpe 失真）
- M2: backtest/engine.py 支持加仓
- M3: preflight._last_trade_date 用 chinese_calendar 倒推
- M4: 删除/标注 alert/notify.py 旧 ETF 推送格式（建议删除 signal 命令）
- M5: 抽 metrics/sharpe.py 统一 backtest 与 simulation 口径
- M6: 新增 latest_market_cap 汇总表，避免 4400 次 SQL
- M7: ml/ranker.py 模型路径改绝对路径
- L1-L5: 测试 mock + 注释更新 + 默认参数改 None + random.seed

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 整体验收清单

- [ ] `python3 server.py`（不传 WEB_TOKEN）应启动报错
- [ ] `python3 -m pytest tests/ -v` 仍 62+ 项全过
- [ ] `grep -rn "5000\b" portfolio/ scripts/ server.py simulation/` 无残留 magic number
- [ ] `grep -rn "20000\.0" data/ simulation/ portfolio/` 无残留硬编码
- [ ] 跑一次 `python3 main.py backtest`，nav_curve 行数应 ≈ 交易日总数（不是 signal 数）
- [ ] `python3 scripts/preflight.py` 在节后第一日不再误报数据不新鲜
- [ ] PROGRESS.md 追加一节记录本轮修复
