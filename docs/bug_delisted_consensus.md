# Bug: 共识选股退市股污染

**发现日期**: 2026-05-09
**修复日期**: 2026-05-09
**严重程度**: 高 (生产环境 D 方案失效)
**状态**: 已修复

## 现象

`daily_scored_cache` 中频次最高的股票全是退市/停牌股，导致 D 方案共识选股选出的 10 只中 9 只为退市股，周一 08:30 推送的标的几乎全部不可交易。

## 根因（勘误：原 v1 表述不准确）

**v1（错）**：以为是 `portfolio/consensus.py` `cache_scored()` 写入时未过滤非活跃股。

**v2（正确）**：真正污染源在 **`scripts/backfill_consensus_cache.py:105`**：

```python
win = df[df["date_str"] <= D].tail(120)   # ← 不限定行的实际日期
```

对 2022 年退市的股票（如 002619），当 `D = 2026-04-17` 时，`tail(120)` 返回的是 2021–2022 那 120 行**冻结历史数据**——长度满足 `>= 20`，于是带着穿越的旧因子进了打分流程，最终写进 cache。

live 路径 `factors/calculator.py:178` 用绝对日期范围过滤窗口，没这个 bug；所以只有 backfill 跑出来的 cache 受污染，每天 cron 跑 monitor-only 维护的 cache 是干净的。

## 实测证据

cache 中 freq=10 的退市股（002619 / 600393 / 000540 / 000961 / 000413 / 601258 / 002505 / 000040）全部出现在 **2026-04-17 ~ 2026-04-30** 这段——正好对应 backfill 跑的时间段；2026-05-02 那次 live 跑的 cache 干净。

cache top 10 退市股频次（修复前快照）：

| 代码 | 频次 | 最后交易日 |
|------|------|-----------|
| 000982 | 10 | 2024-06-21 |
| 600393 | 10 | 2023-06-08 |
| 000413 | 10 | 2024-08-14 |
| 600677 | 10 | 2020-04-29 |
| 002619 | 10 | 2022-03-31 |
| 000961 | 10 | 2024-05-08 |
| 600321 | 10 | 2024-05-30 |

## 修复

### 1. 主修复 — backfill 新鲜度守卫
`scripts/backfill_consensus_cache.py:105` 后加：

```python
last_bar = pd.to_datetime(win.iloc[-1]["date_str"])
if (pd.to_datetime(D) - last_bar).days > 7:
    continue
```

窗口最末一根 bar 距目标日 `D` 超过 7 天则视为非活跃股票跳过。

### 2. 兜底 — `cache_scored` 写入前过滤
`portfolio/consensus.py` 增加 `_is_active(conn, code, date)` 工具，在 `cache_scored()` 入库前检查每个 code 在 `stock_{code}` 表里的最近 bar 是否距 `date` ≤ 7 天，否则跳过并 warn。双保险。

### 3. 同款漏洞 — 回测脚本
`scripts/backtest_year.py:135` 是同款 `tail(120)` 不校验最末 bar，已加同样的新鲜度守卫。回测中 `fwd_return` 天然剔除了退市股的收益贡献，所以绝对收益不会被夸大；但横截面 winsorize / z-score 受陈旧因子拉偏，因子排名失真，alpha 有高估倾向。

### 4. 清理污染 cache
```sql
DELETE FROM daily_scored_cache WHERE date <= '2026-04-30';
```
保留 2026-05-02 干净 cache。备份在 `data/backup/daily_scored_cache_polluted_20260509.csv`。

## 经验

- `tail(N)` 在退市/停牌场景下会静默返回**陈旧窗口**，长度检查 `len >= 20` 形同虚设——必须额外校验最末 bar 的**实际日期**。
- 生产 live 路径用绝对日期范围过滤是正确写法（`factors/calculator.py:178`），backfill / 回测脚本应该对齐这一约定，而不是图便宜用 `tail(N)`。
- 同样的 bug 模式可能潜伏在其他 `tail(...)` 调用里，未来如果新增 backfill 或离线评分脚本，code review 时优先查这个模式。
