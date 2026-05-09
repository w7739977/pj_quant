# Bug: 共识选股退市股污染

**发现日期**: 2026-05-09
**严重程度**: 高 (生产环境 D 方案失效)
**状态**: 待修复

## 问题

`daily_scored_cache` 中频次最高的股票全是退市/停牌股，导致 D 方案共识选股选出的 10 只中 9 只是退市股。

## 原因

1. 退市股因子数据冻结在最后交易日，得分永远不变
2. `cache_scored()` 写入 top 10 时未过滤非活跃股
3. `consensus_picks()` 按频次排序，退市股每次都在 top 10，频次最高

## 实测数据

cache top 10 频次排名（全部退市）：

| 代码 | 频次 | 最后交易日 |
|------|------|-----------|
| 000982 | 10 | 2024-06-21 |
| 600393 | 10 | 2023-06-08 |
| 000413 | 10 | 2024-08-14 |
| 600677 | 10 | 2020-04-29 |
| 002619 | 10 | 2022-03-31 |
| 000961 | 10 | 2024-05-08 |
| 600321 | 10 | 2024-05-30 |

D 方案 2026-05-06 共识选出的 10 只中，仅 000609 一只活跃（收益 +5.0%）。

## 影响

- 周一 08:30 共识选股推送的标的几乎全部不可交易
- 回测中 D 方案的 alpha 可能被高估（回测脚本有 `fwd_return` 天然过滤了无数据股票）

## 修复方案

### 方案 A: 写入时过滤（推荐）

`cache_scored()` 在写入前检查股票是否仍在交易：

```python
# consensus.py cache_scored() 中
from data.storage import load_stock_daily

def _is_active(code, date_str):
    df = load_stock_daily(code)
    if df is None or df.empty:
        return False
    latest = df["date"].astype(str).str[:10].max()
    return latest >= date_str

# 入库前过滤
rows = [r for r in rows if _is_active(r[1], date)]
```

### 方案 B: 读取时过滤

`consensus_picks()` 返回结果后，在 `allocator.py` 调用处过滤：

```python
# allocator.py get_stock_picks_consensus() 中
cons = consensus_picks(end_date=today, window=window, top_n=top_n * 3)
# 过滤非活跃
from data.storage import load_stock_daily
active_cons = [c for c in cons if is_tradeable(c["code"])]
```

### 方案 C: 两者都做（最安全）

写入过滤 + 读取过滤双保险，并清理已有 cache。

## 后续清理

修复后需执行：

```sql
-- 清理 daily_scored_cache 中的退市股
-- 方案: 删除全部旧 cache，让系统重新积累干净数据
DELETE FROM daily_scored_cache;
```

或用 backfill 脚本重填（如有）。
