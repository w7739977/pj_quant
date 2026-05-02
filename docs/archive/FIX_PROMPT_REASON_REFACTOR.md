# 修复 Prompt — humanize_reason 结构化重构 + 资金流配套

> 分支：`feature/simulated-trading`（基于最新 commit `e341282`）。  
> 目标：把 main 上 4 月初已验证的 `reason_text.py` 集中重构合并到本分支，让 e341282 新增的资金流向走结构化数据，而不是再次正则反解析自家拼字符串。  
> 工程量约 30-45 分钟，一个 commit 完成。

---

## 项目背景（精简）

- main 分支上做过一次 reason 文案重构（FIX_PROMPT_2 的 Fix 2.3 + NB2/NB3）：
  - 新建 `portfolio/reason_text.py` 集中处理结构化 dict
  - allocator.py 把因子值/排名/ML 预测打包成 `reason_data` dict 随 pick 传出
  - engine.py 的 `_generate_next_plan` 把 `reason_data` 拷进 `plan["buys"]`
  - engine.py 的 `_execute_order` 调 `save_trade(..., reason_data=...)` 入库
  - `simulation/trade_log.save_trade` 接受 dict 或 str（自动 JSON 化）
  - `trade_utils.humanize_reason` 与 `report._humanize_reason` 都改为优先用 dict
- simulated-trading 分支当时没合并这部分，现在 e341282 新增的资金流向又走了"reason 字符串 + 正则反解析"老路，且 `pick["capital_flow"]` dict 写而不读

## 当前问题盘点（review 发现）

| ID | 问题 | 文件:行 |
|----|------|---------|
| H1 | `humanize_reason` 用正则解析自家拼字符串（资金流逻辑） | `portfolio/trade_utils.py:103-124` |
| H1' | 同样代码在 `simulation/report.py` 重复一份 | `simulation/report.py:133-154` |
| H2 | `pick["capital_flow"]` 结构化 dict 写而不读 | `portfolio/allocator.py:514` 写入；下游忽略 |
| M2 | Tushare moneyflow 取最近交易日时不识别法定节假日 | `data/fetcher.py:74-87` |
| M5 | "净流出"分支不展示超大单/大单明细（只流入展示） | `trade_utils.py:120-122`, `report.py:151-153` |

---

## Phase 1：新建 `portfolio/reason_text.py`（约 5 分钟）

**完整内容**（基于 main 现有版本 + 资金流扩展）：

```python
"""
统一理由文案生成 — 结构化 dict 输入，无正则

所有模块（终端/微信/模拟盘日报）共用此函数，避免回到"先字符串化再正则反解析"反模式。
"""

import re
from typing import Optional


def humanize_reason(reason_data: dict, name: str = "",
                    fallback_reason: str = "") -> str:
    """
    将结构化因子数据翻译成通俗易懂的理由

    Parameters
    ----------
    reason_data : dict
        结构化因子数据，含 factor_rank / ml_rank / in_both / key_factors /
        predicted_return / capital_flow 等
    name : str
        股票名称（前缀）
    fallback_reason : str
        降级用原始 reason 字符串（当 reason_data 为空时调用 legacy 正则）
    """
    if not reason_data:
        if fallback_reason:
            return _legacy_humanize(fallback_reason, name)
        return ""

    parts = []

    # 排名
    factor_rank = reason_data.get("factor_rank")
    ml_rank = reason_data.get("ml_rank")
    in_both = reason_data.get("in_both", False)
    if factor_rank is not None and ml_rank is not None:
        fr, mr = int(factor_rank), int(ml_rank)
        if in_both:
            parts.append("多因子和ML模型均排名靠前，信号强烈")
        elif fr <= 20:
            parts.append(f"多因子排名第{fr}，技术面优势明显")
        elif mr <= 20:
            parts.append(f"ML模型预测排名第{mr}，看好后续走势")
        else:
            parts.append(f"多因子第{fr}、ML第{mr}")

    # 关键因子
    kf = reason_data.get("key_factors", {})

    mom_20d = kf.get("mom_20d")
    if mom_20d is not None:
        try:
            v = float(mom_20d) * 100
            if v > 15:
                parts.append(f"短期强势(20日涨{v:.0f}%)")
            elif v > 5:
                parts.append(f"温和上涨(20日涨{v:.0f}%)")
            elif v < -10:
                parts.append(f"短期弱势(20日跌{abs(v):.0f}%)")
        except (ValueError, TypeError):
            pass

    pe = kf.get("pe_ttm")
    if pe is not None:
        try:
            v = float(pe)
            if v < 0:
                parts.append("亏损股")
            elif v < 15:
                parts.append(f"低估值(PE仅{v:.0f})")
            elif v > 50:
                parts.append(f"估值偏高(PE={v:.0f})")
        except (ValueError, TypeError):
            pass

    pb = kf.get("pb")
    if pb is not None:
        try:
            v = float(pb)
            if v < 1:
                parts.append(f"破净(PB={v:.1f})")
            elif v < 3:
                parts.append(f"估值合理(PB={v:.1f})")
        except (ValueError, TypeError):
            pass

    # ML 预测收益
    pred_ret = reason_data.get("predicted_return")
    if pred_ret is not None:
        try:
            v = float(pred_ret) * 100
            if v > 3:
                parts.append(f"模型预测看涨(+{v:.0f}%)")
            elif v < -3:
                parts.append(f"模型预测有风险({v:.0f}%)")
        except (ValueError, TypeError):
            pass

    # 主力资金流向（M5: 流入流出都展示明细）
    cf = reason_data.get("capital_flow")
    if cf:
        flow_part = _format_capital_flow(cf)
        if flow_part:
            parts.append(flow_part)

    if parts:
        prefix = f"{name}：" if name else ""
        return f"{prefix}{'，'.join(parts)}"
    return fallback_reason


def _format_capital_flow(cf: dict) -> str:
    """格式化资金流向，流入流出都展示超大单/大单明细"""
    mf = cf.get("net_mf_amount", 0) or 0
    elg = cf.get("elg_net", 0) or 0
    lg = cf.get("lg_net", 0) or 0

    direction = "净流入" if mf >= 0 else "净流出"
    main_str = f"主力{direction}{_fmt_amount(abs(mf))}"

    detail_parts = []
    if abs(elg) >= 1:
        sign = "+" if elg >= 0 else "-"
        detail_parts.append(f"超大单{sign}{_fmt_amount(abs(elg))}")
    if abs(lg) >= 1:
        sign = "+" if lg >= 0 else "-"
        detail_parts.append(f"大单{sign}{_fmt_amount(abs(lg))}")

    if detail_parts:
        suffix = "资金积极做多" if mf >= 0 else "注意资金抛压"
        return f"{main_str}({', '.join(detail_parts)})，{suffix}"
    suffix = "资金看好" if mf >= 0 else "注意风险"
    return f"{main_str}，{suffix}"


def _fmt_amount(wan_yuan: float) -> str:
    """万元 → 可读金额（无符号）"""
    if wan_yuan >= 10000:
        return f"{wan_yuan / 10000:.1f}亿"
    elif wan_yuan >= 100:
        return f"{wan_yuan:.0f}万"
    return f"{wan_yuan:.1f}万"


# ---- legacy: 正则解析旧格式 reason 字符串（仅作 fallback）----

def _legacy_humanize(reason: str, name: str = "") -> str:
    """用正则解析自家拼出的字符串（fallback，不应是主路径）"""
    if not reason:
        return ""

    if any(kw in reason for kw in ["止损", "止盈", "超时调仓", "调仓换股"]):
        return reason

    parts = []

    factor_match = re.search(r"因子#(\d+)", reason)
    ml_match = re.search(r"ML#(\d+)", reason)
    both = "★双重确认" in reason
    if factor_match and ml_match:
        fr, mr = int(factor_match.group(1)), int(ml_match.group(1))
        if both:
            parts.append("多因子和ML模型均排名靠前，信号强烈")
        elif fr <= 20:
            parts.append(f"多因子排名第{fr}，技术面优势明显")
        elif mr <= 20:
            parts.append(f"ML模型预测排名第{mr}，看好后续走势")
        else:
            parts.append(f"多因子第{fr}、ML第{mr}")

    for key in ("mom_20d", "pe_ttm", "pb"):
        m = re.search(rf"{key}:([+-]?\d+\.?\d*%?)", reason)
        if not m:
            continue
        val = m.group(1)
        if key == "mom_20d":
            try:
                v = float(val.replace("%", ""))
                if v > 15:
                    parts.append(f"短期强势(20日涨{v:.0f}%)")
                elif v > 5:
                    parts.append(f"温和上涨(20日涨{v:.0f}%)")
                elif v < -10:
                    parts.append(f"短期弱势(20日跌{abs(v):.0f}%)")
            except ValueError:
                pass
        elif key == "pe_ttm":
            try:
                v = float(val)
                if v < 0:
                    parts.append("亏损股")
                elif v < 15:
                    parts.append(f"低估值(PE仅{v:.0f})")
                elif v > 50:
                    parts.append(f"估值偏高(PE={v:.0f})")
            except ValueError:
                pass
        elif key == "pb":
            try:
                v = float(val)
                if v < 1:
                    parts.append(f"破净(PB={v:.1f})")
                elif v < 3:
                    parts.append(f"估值合理(PB={v:.1f})")
            except ValueError:
                pass

    pred_match = re.search(r"预测20日收益:([+-]?\d+\.?\d*%?)", reason)
    if pred_match:
        try:
            v = float(pred_match.group(1).replace("%", ""))
            if v > 3:
                parts.append(f"模型预测看涨(+{v:.0f}%)")
            elif v < -3:
                parts.append(f"模型预测有风险({v:.0f}%)")
        except ValueError:
            pass

    # 资金流（legacy 也补全流出明细）
    flow_match = re.search(r"资金:(.+?)(?:\n|$)", reason)
    if flow_match:
        flow_str = flow_match.group(1)
        mf_m = re.search(r"主力净(流入|流出)([\d.]+[亿万])", flow_str)
        if mf_m:
            direction, amount = mf_m.group(1), mf_m.group(2)
            elg_m = re.search(r"超大单([+-]?[\d.]+[亿万])", flow_str)
            lg_m = re.search(r"(?<!超)大单([+-]?[\d.]+[亿万])", flow_str)
            details = []
            if elg_m:
                details.append(f"超大单{elg_m.group(1)}")
            if lg_m:
                details.append(f"大单{lg_m.group(1)}")
            suffix = "资金积极做多" if direction == "流入" else "注意资金抛压"
            if details:
                parts.append(f"主力{direction}{amount}({', '.join(details)})，{suffix}")
            else:
                parts.append(f"主力{direction}{amount}，{suffix}")

    if parts:
        prefix = f"{name}：" if name else ""
        return f"{prefix}{'，'.join(parts)}"
    return reason
```

---

## Phase 2：`portfolio/allocator.py` 让 picks 携带 `reason_data`（约 5 分钟）

**位置**：`get_stock_picks_live` 函数，picks.append 块（当前在 e341282 之前的 Step 5）。

修改前后对照：

```python
# 修改前（约 L483-498）
picks.append({
    "code": code,
    "name": name,
    "shares": share_info["shares"],
    "price": price,
    "amount": amount,
    "cost": cost,
    "reason": reason,
    "final_score": round(float(row["final_score"]), 2),
    "dimension_scores": {...},
})
```

```python
# 修改后：增加 reason_data 字段
picks.append({
    "code": code,
    "name": name,
    "shares": share_info["shares"],
    "price": price,
    "amount": amount,
    "cost": cost,
    "reason": reason,
    "reason_data": {
        "factor_rank": factor_rank,
        "ml_rank": ml_rank,
        "in_both": bool(row["in_both"]),
        "key_factors": {
            "mom_20d": row.get("mom_20d"),
            "pe_ttm": row.get("pe_ttm"),
            "pb": row.get("pb"),
            "vol_10d": row.get("vol_10d"),
            "turnover_rate": row.get("turnover_rate"),
        },
        "predicted_return": (
            pred_row.iloc[0].get("predicted_return")
            if not pred_row.empty else None
        ),
        # capital_flow 在 Step 6 之后回填
    },
    "final_score": round(float(row["final_score"]), 2),
    "dimension_scores": {...},
})
```

**Step 6（资金流向回填）改造**：把已有的 `p["capital_flow"] = flow` 同时写入 `reason_data["capital_flow"]`，并**删掉** "p["reason"] += f" | 资金:..."" 这一行（不再依赖字符串拼接）：

```python
# Step 6 修改后
if picks:
    try:
        from data.fetcher import fetch_capital_flow_batch
        pick_codes = [p["code"] for p in picks]
        flow_data = fetch_capital_flow_batch(pick_codes)
        for p in picks:
            flow = flow_data.get(p["code"])
            if flow:
                p["capital_flow"] = flow  # 保留兼容
                # 同步进 reason_data
                if "reason_data" in p and isinstance(p["reason_data"], dict):
                    p["reason_data"]["capital_flow"] = flow
        if flow_data:
            print(f"  资金流向: {len(flow_data)}/{len(pick_codes)} 只获取成功")
    except Exception as e:
        logger.warning(f"资金流向获取失败(非关键): {e}")
```

---

## Phase 3：`simulation/engine.py` 让 plan 携带 reason_data（约 5 分钟）

### 3.1 `_generate_next_plan` 拷贝 picks 时加上 reason_data

定位 `picks → plan["buys"]` 拷贝循环（应在 L820 附近），增加：

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

### 3.2 `_execute_order` 把 reason_data 传给 save_trade

定位 `save_trade(...)` 调用处（L450 左右），增加 `reason_data` 参数：

```python
def _get_order_reason_data(self, order) -> dict:
    """从计划中获取订单对应的 reason_data（dict）"""
    for buy in self.daily_plan.get("buys", []):
        if buy.get("code") == order.symbol:
            rd = buy.get("reason_data")
            if rd:
                return rd if isinstance(rd, dict) else {}
    return {}
```

```python
# _execute_order 中 save_trade 调用改为：
self.save_trade(
    order.symbol, name, "buy", shares, price, amount, fee,
    reason=order.reason, order_id=order.order_id,
    reason_data=self._get_order_reason_data(order),
)
```

---

## Phase 4：`simulation/trade_log.py` 增加 reason_data 列（约 5 分钟）

### 4.1 schema 增加列（idempotent ALTER）

```python
# _get_conn 内部 CREATE TABLE 增加：
CREATE TABLE IF NOT EXISTS sim_trades (
    ...
    reason_data TEXT DEFAULT '{}'    # 新增
);
```

对老库做幂等迁移：

```python
def _ensure_reason_data_column(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sim_trades)").fetchall()}
    if "reason_data" not in cols:
        conn.execute("ALTER TABLE sim_trades ADD COLUMN reason_data TEXT DEFAULT '{}'")
        conn.commit()
```

在 `_get_conn()` 末尾调用此函数。

### 4.2 `save_trade` 接受 dict 或 str

```python
def save_trade(symbol, name, side, shares, price, amount, fee, *,
               profit=0.0, reason="", order_id=0, reason_data=None):
    """写入成交记录"""
    # reason_data: 接受 dict 或 str，统一序列化为 JSON 字符串
    if isinstance(reason_data, dict):
        reason_data = json.dumps(reason_data, ensure_ascii=False)
    elif not reason_data:
        reason_data = "{}"
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO sim_trades
               (date, symbol, name, side, shares, price, amount, fee,
                profit, reason, order_id, reason_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             symbol, name, side, shares, price, amount, fee,
             profit, reason, order_id, reason_data),
        )
        conn.commit()
    finally:
        conn.close()
```

---

## Phase 5：`portfolio/trade_utils.py` 删 regex + 改签名（约 5 分钟）

### 5.1 `humanize_reason` 改为代理到 reason_text

```python
def humanize_reason(reason: str, name: str = "", reason_data: dict = None) -> str:
    """优先用 reason_data dict，无则降级用 reason 字符串正则解析"""
    from portfolio.reason_text import humanize_reason as _humanize
    return _humanize(reason_data or {}, name=name, fallback_reason=reason)
```

**删掉**当前文件里所有从 L22 开始的旧 humanize_reason 实现 + L100-124 的资金流 regex 段。

### 5.2 `format_checklist` / `format_push_message` 4 处调用补 reason_data 参数

```python
# 4 处都从
reason_str = humanize_reason(a.get('reason', ''), a.get('name', ''))
# 改为
reason_str = humanize_reason(
    a.get('reason', ''), a.get('name', ''),
    reason_data=a.get('reason_data'),
)
```

注意 sell_actions 没有 reason_data，传 None 走 legacy fallback 是正确行为。

---

## Phase 6：`simulation/report.py` 改用结构化（约 5 分钟）

### 6.1 `_humanize_reason` 改为优先用 trade["reason_data"]

```python
def _humanize_reason(trade: dict) -> str:
    reason = trade.get("reason", "")
    if not reason:
        return ""

    name = trade.get("name", trade.get("symbol", ""))

    # 卖出理由保持原样
    if any(kw in reason for kw in ["止损", "止盈", "超时调仓", "调仓换股"]):
        return reason

    # 优先用结构化数据
    reason_data = trade.get("reason_data")
    if isinstance(reason_data, str):
        try:
            import json
            reason_data = json.loads(reason_data)
        except (json.JSONDecodeError, TypeError):
            reason_data = None

    from portfolio.reason_text import humanize_reason as _humanize
    result = _humanize(reason_data or {}, name=name, fallback_reason=reason)

    # 维度得分（如有）
    dim_scores = trade.get("dimension_scores")
    if dim_scores:
        dim_str = _format_dimension_scores(dim_scores, compact=True)
        if dim_str:
            result += f"\n    得分: {dim_str}"

    return result
```

**删掉**当前文件 L37-160 的旧实现 + L130-154 的资金流 regex 段。

---

## Phase 7：M2 顺手修复 — `data/fetcher.py` 节假日感知（约 3 分钟）

```python
# _fetch_capital_flow_tushare 中 fallback 路径
if not trade_date:
    from datetime import timedelta
    d = datetime.now().date() - timedelta(days=1)
    try:
        import chinese_calendar
        while not chinese_calendar.is_workday(d) or d.weekday() >= 5:
            d -= timedelta(days=1)
    except Exception:
        # chinese_calendar 不可用降级到周末判断
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    trade_date = d.strftime("%Y%m%d")
```

---

## 整体验收清单

```bash
# 1. 烟囱测试: 确认 import 链不破
python3 -c "
from portfolio.reason_text import humanize_reason
from portfolio.trade_utils import humanize_reason as hu
from simulation.report import _humanize_reason
from simulation.engine import SimEngine
SimEngine()
print('imports OK')
"

# 2. 验证结构化优先
python3 -c "
from portfolio.reason_text import humanize_reason
result = humanize_reason({
    'factor_rank': 3, 'ml_rank': 5, 'in_both': True,
    'key_factors': {'mom_20d': 0.20, 'pe_ttm': 11.8, 'pb': 0.9},
    'predicted_return': 0.031,
    'capital_flow': {'net_mf_amount': 5000, 'elg_net': 12000, 'lg_net': -3000},
}, name='测试股')
print(result)
"
# 预期输出含: 多因子和ML模型均排名靠前 + 短期强势(20日涨20%) + 低估值 + 破净
#   + 模型预测看涨(+3%) + 主力净流入5000万(超大单+1.2亿, 大单-3000万)，资金积极做多

# 3. 验证 legacy fallback
python3 -c "
from portfolio.reason_text import humanize_reason
fallback = '因子#3 ML#5 ★双重确认 | mom_20d:+20.3% pe_ttm:11.8 | 资金:主力净流入5000万,超大单+1.2亿'
result = humanize_reason({}, name='测试股', fallback_reason=fallback)
print(result)
"
# 预期输出与上面 dict 路径基本一致

# 4. 全量测试
python3 -m pytest tests/ -q   # 应保持原 57 项通过
echo == Run 2 ==
python3 -m pytest tests/ -q

# 5. 残留正则检查
grep -n "re\.search.*因子#\|re\.search.*资金:" portfolio/trade_utils.py simulation/report.py
# 预期: 无输出（regex 全部移到 reason_text._legacy_humanize 内部）
```

### 验收点检查
- [ ] `portfolio/reason_text.py` 已创建，含 `humanize_reason` + `_legacy_humanize`
- [ ] `portfolio/allocator.py:get_stock_picks_live` picks 含 `reason_data` 字段
- [ ] Step 6 把 capital_flow 同步进 `reason_data`，**移除** `p["reason"] += " | 资金:..."` 拼接
- [ ] `simulation/engine.py:_generate_next_plan` plan["buys"] 含 reason_data
- [ ] `simulation/engine.py:_execute_order` 调 `save_trade(reason_data=...)`
- [ ] `simulation/trade_log.py` schema 加 reason_data 列 + idempotent ALTER 迁移
- [ ] `portfolio/trade_utils.py` humanize_reason 改为 3 参数（reason, name, reason_data）
- [ ] 4 处调用站点（format_checklist 卖/买 + format_push_message 卖/买）补 reason_data 参数
- [ ] `simulation/report.py:_humanize_reason` 优先读 trade["reason_data"]
- [ ] `data/fetcher.py:_fetch_capital_flow_tushare` 节假日感知
- [ ] M5: 流出场景展示超大单/大单明细
- [ ] pytest 连续 2 次全过
- [ ] grep 检查无残留 regex

---

## 提交规范

一个 commit 完成：

```
refactor: humanize_reason 结构化重构 + 资金流走 dict 不走正则

合并 main 分支 4 月初的 reason_text.py 重构思路到 simulated-trading：
- 新建 portfolio/reason_text.py 集中处理结构化 dict（H1）
- allocator.py picks 增加 reason_data 字段，整合 capital_flow（H2）
- engine.py _generate_next_plan + _execute_order 串联 reason_data 数据链
- trade_log.py schema 增加 reason_data 列（含 idempotent ALTER 迁移）
- trade_utils.py humanize_reason 改 3 参数签名，4 处调用补 reason_data
- report.py _humanize_reason 优先读 trade["reason_data"]
- 删除 trade_utils + report.py 两段重复的资金流 regex 解析
- M5: 资金流出场景补充超大单/大单明细
- M2: fetcher.py moneyflow trade_date fallback 节假日感知

效果: e341282 已写入但忽略的 pick["capital_flow"] dict 现在被下游正确消费，
未来加新因子（fund-flow-factor 分支的 5日均值因子等）只需在 picks 里加字段，
不再撞 reason 字符串拼接的 ` | ` 分隔符。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 不在本次范围

- M1 资金流 SQLite 持久化 → 推到 `feature/fund-flow-factor` 分支统一处理
- M3 Tushare/东方财富时段一致 → 数据底座建好后随之消失
- M4 东方财富兜底并发 → 性能优化，picks 数量小不影响日常使用
- L1-L4 风格类问题 → 顺手能改但不强求，重点在结构化重构闭环
