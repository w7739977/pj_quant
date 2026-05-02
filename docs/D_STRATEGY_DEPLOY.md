# D 方案部署指南（5 天频次共识选股）

> 2026-05-02 起的生产部署方案，配合云主机自动跑。

---

## 一、最小部署清单

### 1.1 代码 + 数据
```bash
# 拉代码
git clone https://github.com/w7739977/pj_quant.git
cd pj_quant
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 配置密钥
cp config/settings.py.example config/settings.py
# 编辑 settings.py 填入：TUSHARE_TOKEN / PUSHPLUS_TOKEN / LLM_API_KEY
```

### 1.2 ⚠️ 模型文件（必读）

**`ml/models/*.json` 已在 `.gitignore` 中，仓库不含训练好的模型**。新机器部署需做以下任一选项：

| 选项 | 操作 | 时长 |
|---|---|---|
| **A. 本机训练** | `python3 main.py evolve` 一键完成 | ~5 分钟 |
| **B. 复制现有模型** | `scp ml/models/xgb_ranker.json user@host:/path/to/pj_quant/ml/models/` | 几秒 |

**最小集（生产模型）**：
```
ml/models/xgb_ranker.json           # 必须，生产模型
ml/models/feature_importance.json   # 可选，特征重要性
ml/models/model_history.json        # 可选，版本历史
```

---

## 二、首次部署完整流程

按顺序执行：

```bash
# 1. 增量数据（行情 + daily_basic 估值）
python3 main.py fetch-all --incremental    # ~30 分钟全量首次，之后每日增量 5-10 分钟

# 2. 行业分类（一次性，月度更新）
python3 main.py fetch-industry

# 3. 财务因子 PIT（一次性，季度更新）
python3 main.py fetch-financial            # ~30 分钟，5728 只股票

# 4. 财务数据质检
python3 scripts/validate_financial.py      # 必须 ✅ 全部检查通过

# 5. 训练 ML 模型
python3 main.py evolve                     # ~5 分钟，生成 xgb_ranker.json

# 6. 回填共识缓存（首次部署必跑，避免等 5 个工作日）
python3 scripts/backfill_consensus_cache.py --days 10

# 7. 端到端冒烟（不推送）
python3 main.py live --monitor-only        # 跑完整流程但不推送

# 8. 配置 crontab 自动化
crontab -e   # 见下方
```

---

## 三、定时任务（生产 crontab）

```cron
# 每日 15:30 — 周一选共识、周二~五 monitor-only
30 15 * * 1-5 /opt/pj_quant/run_daily.sh >> /opt/pj_quant/logs/cron.log 2>&1

# 每月 1 号 16:00 — 自动 evolve 训练
0 16 1 * * cd /opt/pj_quant && python3 main.py evolve --push >> logs/evolve.log 2>&1
```

`run_daily.sh` 内部按周几自动切换：

| 周几 | 命令 | 行为 |
|---|---|---|
| 周一 | `live --consensus --push` | 5 天频次共识选股 + 推送清单 |
| 周二~五 | `live --monitor-only --push` | 止损/止盈监控 + 缓存今日 scored 供下周共识 |
| 周六/日 | crontab `1-5` 自动跳过 | — |

---

## 四、D 方案核心机制

```
┌─────────────────────────────────────────────────┐
│  每个工作日 (周一到周五):                          │
│   1. 跑因子 + ML 预测 + 50/50 加权打分 (~3 min)   │
│   2. cache 当日 top 10 final_score 到 SQLite     │
│                                                 │
│  周一额外:                                       │
│   3. 读取过去 5 个交易日 cache                    │
│   4. 频次共识排序 (出现次数 → 平均得分)            │
│   5. 取 top 10 → 推送实盘清单                     │
└─────────────────────────────────────────────────┘
```

**实证（2026-01-01 ~ 04-23 共 13 周回测）**：

| 方案 | avg_alpha | 累计 alpha | sharpe-like | 跑赢基准率 |
|---|---|---|---|---|
| 日频 | +0.41% | — | +0.15 | 52.8% |
| **D. 共识** | **+1.15%** | **+15.69%** | **+0.50** | **69.2%** |

---

## 五、生产环境验证

### 5.1 模型健康度
```bash
python3 main.py evolve-history    # 查看最近 5 次 evolve 记录
```

预期：当前模型 `R² ≥ 0.07`、样本 ≥ 10 万、top 5 因子合理（avg_turnover_5d / vol_10d / mom_*等）。

### 5.2 共识缓存就绪
```bash
python3 -c "
import sys; sys.path.insert(0, '.')
from portfolio.consensus import cache_stats
print(cache_stats())
"
```

期望：`distinct_dates >= 5`、`max_date` 接近今天。

**❗ 首次部署/迁移机器：用 backfill 脚本一次性补足缓存**（避免等 5 个工作日累积）：

```bash
python3 scripts/backfill_consensus_cache.py --days 10
```

这会用当前生产模型对过去 10 个交易日重算 final_score 入 cache。完成后下次 `live --consensus` 直接可用。

### 5.3 端到端
```bash
# 周一前一周（周日）手动触发一次共识，看是否推送
python3 main.py live --consensus --simulate    # --simulate 不动真实持仓
```

---

## 六、回滚与灾备

### 6.1 关掉 D 方案，回到日频
临时修改 `run_daily.sh`：
```bash
LIVE_ARGS="live --push"     # 删 --consensus / --monitor-only
```

### 6.2 模型损坏
```bash
ls ml/models/xgb_ranker_*.json    # 历史备份
# 选一个备份恢复
cp ml/models/xgb_ranker_20260502_124329.json ml/models/xgb_ranker.json
```

### 6.3 数据库损坏
```bash
ls -la data/quant.db
# 用最近的备份替换；没备份则重新拉数据
python3 main.py fetch-all
python3 main.py fetch-financial
python3 main.py fetch-industry
```

---

## 七、常见问题

### Q1：周一推送了但 picks 为空？
- 检查 `cache_stats()`：`distinct_dates < 5` → 共识缓存不足，自动回退到日频
- 解决：先跑 4-5 个工作日的 monitor-only 累积缓存

### Q2：R² 突然下跌？
- `python3 main.py evolve-history` 看趋势
- 排查：(a) 池子是否变化（北交所是否被错误纳入）；(b) 财务数据是否有新异常值
- 兜底：rollback 到上一版备份模型

### Q3：北交所股票出现在选股结果？
- 不应该。`is_tradeable()` 已黑名单 4xx/8xx/92x
- 如果出现，检查 `portfolio/trade_utils.py:_BLACKLIST_PREFIXES`

### Q4：换手成本看起来很高？
- 周频换手 ~30%/周 vs 日频 ~50%/天，**周频已经比日频省 4-5 倍成本**
- 实测周频年化摩擦约 8-12%，日频约 30-40%

---

## 八、参考文档

- 通用部署：`DEPLOY.md`
- 优化历史 + 已废弃方案：`docs/optimization_backlog.md`
- 项目进度：`PROGRESS.md`
- D 方案算法：`portfolio/consensus.py` (含完整 docstring)
