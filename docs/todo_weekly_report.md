# TODO: 实盘周五周报推送

**发现日期**: 2026-05-09
**优先级**: 中
**状态**: 待实现

## 背景

用户反馈周五应该收到周报推送（持仓收益 + 下周预告），但 2026-05-08（周五）未推送。

## 原因

1. 当前定时任务无周五周报逻辑
2. `simulation/report.py` 中的 `weekly_report()` 是模拟盘周报，读 `sim_trading.db`，与实盘无关
3. 实盘周报从未实现

## 当前定时任务

| 任务 | 时间 | 内容 |
|------|------|------|
| `run_weekly.sh` | Mon-Fri 08:30 | 仅本周第一个交易日跑共识选股 |
| `run_daily.sh` | Mon-Fri 15:15 | 每日持仓监控推送 |
| 月初 evolve | 每月1号 16:00 | 自动训练 |

## 实盘周报应包含的内容

### 数据源（实盘）

- `data/quant.db` → `daily_scored_cache`（共识缓存）
- `data/portfolio.json` → 实盘持仓（通过 `data.storage.load_portfolio()`）
- `logs/signals/*.json` → 每日信号归档
- `ml/models/` → 模型状态

### 周报内容

1. **持仓状态**: 当前持仓、本周收益、累计盈亏
2. **上周共识选股验证**: 上周一选出的 top 10 本周实际收益
3. **每日信号 T+1 验证**: 每日 top 10 的次日表现
4. **大盘基准**: 上证、创业板同期涨跌
5. **本周情绪曲线**: 每日市场情绪得分 + 关键新闻
6. **下周计划**: 共识缓存状态、模型状态

## 实现方案

1. 创建 `scripts/weekly_report.py`，基于实盘数据源生成周报
2. 在 `run_daily.sh` 中增加周五判断：

```bash
# 阶段四：周五周报（最后一个阶段）
WEEKDAY=$(date +%u)  # 5=周五
if [ "$WEEKDAY" -eq 5 ]; then
    echo "[$(date +%H:%M:%S)] 生成周五周报..." | tee -a "$LOG_FILE"
    python3 scripts/weekly_report.py --push 2>&1 | tee -a "$LOG_FILE" || true
fi
```

3. 推送格式适配手机阅读（Markdown）

## 注意事项

- 依赖先修复 `bug_delisted_consensus.md` 中的退市股问题，否则共识验证部分数据失真
- 周五可能是节前最后一天，需判断是否为本周最后一个交易日
