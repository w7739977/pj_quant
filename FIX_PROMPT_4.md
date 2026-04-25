# 代码修复 Prompt v4 — 收尾 regression + 漏项

> 第三轮验收发现 2 个 regression（R1/R2）和 2 个 FIX_PROMPT_3.md 未实施项（M4/M6）。  
> R1/R2 阻断 CI、文件污染工作区，必须修；M4/M6 是改进项可单独跟进。  
> 建议拆 2 个 commit。

---

## 项目背景（精简）

- 上一轮（commit 0598785 / bf8fad5）实施了 server.py 安全硬化 + 13 项一致性修复
- 验收发现：`pytest tests/test_server.py` 单跑能过，**连续跑两次会失败**（幂等缓存污染）
- `data/.portfolio.lock` 是 H1.d 文件锁副产物，每次保存持仓都 touch，当前在 untracked 列表

---

## R1（必修）：`test_server.py` 幂等测试隔离不足

**症状**：
```bash
$ pytest tests/test_server.py  # 第一次：8 项全过
$ pytest tests/test_server.py  # 第二次：
FAILED tests/test_server.py::TestSync::test_sync_buy - KeyError: 'success'
FAILED tests/test_server.py::TestSync::test_sync_sell_nonexistent - KeyError: 'errors'
```

**根因链**：
1. 测试用例硬编码 `client_request_id="test-buy-001"` 等（`tests/test_server.py:80, 96, 110`）
2. `server._do_sync` 把这些 key 写进 `logs/sync/idempotent_{today}.json` 持久化
3. 同日二次运行 → 命中幂等缓存 → 返回 `{"idempotent": True, "previous_result": ...}`，没 `success` / `errors` 字段
4. `assert data["success"] is True` → KeyError

**复现**：
```bash
rm -f logs/sync/idempotent_*.json
pytest tests/test_server.py -q   # PASS
pytest tests/test_server.py -q   # FAIL（同日二次跑）
```

**修复**（推荐方案 A — 最小改动）：

新建 `tests/conftest.py`：
```python
"""测试公共 fixture"""
import os
import pytest


@pytest.fixture(autouse=True)
def _clean_idempotent_log():
    """每个测试结束后清理幂等日志，确保跨测试 / 跨运行隔离"""
    yield
    sync_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "sync",
    )
    if not os.path.exists(sync_dir):
        return
    for f in os.listdir(sync_dir):
        if f.startswith("idempotent_"):
            try:
                os.remove(os.path.join(sync_dir, f))
            except OSError:
                pass
```

**注意**：
- `autouse=True` 让 fixture 自动应用到所有测试
- 只清 `idempotent_*.json`，不动 `{date}.json` 同步记录（用户可能要看历史）
- 放在 `conftest.py` 而非 `test_server.py`，未来其他测试也能复用

**备选方案 B**（不改 fixture，让测试本身唯一化）：
```python
import uuid
def test_sync_buy(self, ...):
    resp = client.post(
        f"/api/sync?token={TEST_TOKEN}",
        json={
            ...,
            "client_request_id": f"test-buy-{uuid.uuid4()}",
        },
    )
```
缺点：`test_sync_idempotent` 必须保留固定 key 才能验证幂等性，需特殊处理；fixture 方案更通用。

**验收**：
```bash
pytest tests/test_server.py -q   # 第一次 PASS
pytest tests/test_server.py -q   # 第二次仍 PASS
pytest tests/test_server.py -q   # 第三次仍 PASS
pytest tests/ -q                 # 全部 64 项（62 + idempotent fixture 不应破其他测试）
```

---

## R2（必修）：`data/.portfolio.lock` 加入 .gitignore

**症状**：
```bash
$ git status
On branch main
Untracked files:
  data/.portfolio.lock
```

`H1.d` 的 fcntl 文件锁在 `data/.portfolio.lock` 落地空文件。每次保存持仓都 touch，反复污染工作区。

**修复**：`.gitignore` 在 "Data & Models" 段下加：
```
# Concurrency lock
data/.portfolio.lock
```

或者更通用：
```
data/*.lock
```

**验收**：
```bash
ls data/.portfolio.lock        # 应存在
git status                     # untracked 列表里不应出现它
```

---

## 顺手项：PROGRESS.md 追加修复记录

按现有格式（按日期倒序），在文件顶部追加一节：

```markdown
## 2026-04-25 代码 review 闭环修复（5 轮）

### 背景

通过 5 轮代码 review 发现并修复了从 simulation 模块缺失到 server.py 安全漏洞的 30+ 个问题，
包括：
- Phase 1（commit 234c42d）：补回 `simulation/trade_log.py` 缺失文件、Py3.9 类型兼容、
  涨跌停撮合 bug、删除参数错误的 `_simulate_execution`
- Phase 2（commit 35cb34a）：持仓时长改交易日、`humanize_reason` 重构为结构化 dict、
  清理死代码
- Phase 3（commit 85b6895）：抽 `MIN_BUY_CAPITAL` 常量、统一 fetch_quotes_batch import、
  10 项一致性修复
- Phase 4（commit 365401c）：创业板 300xxx 涨跌停限制（误判 10% 修为 20%）、
  reason_data 数据链贯通、模拟盘默认资金读 settings
- Phase 5（commit 0598785）：server.py 安全硬化（强制 token + 幂等性 + HTML 转义 + 文件锁）
- Phase 6（commit bf8fad5）：抹平残留硬编码、回测按日 NAV、节假日感知、统一 Sharpe 公式

### 测试

`pytest tests/ -v`：64 项全过（含新增 test_matcher.py 5 个用例 + test_server.py 3 个用例）
```

（可选，不做也不影响功能）

---

## 待跟进（非本轮必修）

| 项 | 来源 | 状态 |
|----|------|------|
| M4 删除/统一 alert/notify.py 旧 ETF 推送格式 | FIX_PROMPT_3 | 未实施 |
| M6 latest_market_cap 汇总表（4400 次 SQL 性能） | FIX_PROMPT_3 | 未实施 |

**建议**：
- M4 改动小（删 `alert/daily_runner.py` + main.py 的 signal 命令分支）但要确认线上是否有 cron 还在调用 `python main.py signal`，建议先 grep 服务器 crontab
- M6 涉及 schema 演进（要在 `tushare_fundamentals.run()` 末尾加汇总表写入），单独 PR 处理

如果不跟进，至少在 README 或 PROGRESS.md 里注明"已知性能瓶颈"。

---

## 提交建议

一个 commit 收尾：

```
fix(test): 幂等测试隔离 + 锁文件 gitignore

R1: tests/conftest.py 加 autouse fixture 清理 idempotent_*.json
    修复 pytest 二次运行 KeyError: 'success'（幂等缓存污染）
R2: .gitignore 加 data/.portfolio.lock（H1.d 文件锁副产物）
+ PROGRESS.md 追加 5 轮 review 修复总结

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 整体验收清单

- [ ] `rm -f logs/sync/idempotent_*.json && pytest tests/test_server.py && pytest tests/test_server.py` 两次都 PASS
- [ ] `pytest tests/ -q` 全部通过（64+ 项）
- [ ] `git status` 不再看到 `data/.portfolio.lock` 在 untracked
- [ ] 跑一次 `python3 server.py`（设了 WEB_TOKEN），手动调一次 `/api/sync`，确认幂等行为正常
- [ ] PROGRESS.md 顶部含本轮总结（可选）
