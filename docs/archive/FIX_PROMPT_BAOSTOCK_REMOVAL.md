# 修复 Prompt — 移除 BaoStock，统一以 Tushare 为数据源

> 分支：`feature/simulated-trading`（基于 commit `ae4f63c`）
> 触发场景：2026-05-01 16:00 cron evolve 失败，决策 `ABORT: 训练样本不足 (0 < 50)`
> 工程量约 60 分钟，分 3 个 commit。

---

## 项目背景（精简）

- 数据底座已经迁移到 Tushare：`data/tushare_daily.py`（K 线，按日期批量，9 分钟全市场）+ `data/tushare_fundamentals.py`（daily_basic，含 pe_ttm/pb/turnover_rate/total_mv 等）
- 实时行情走腾讯免费接口（`fetch_realtime_tencent_batch`），不限流
- 历史 K 线 4400+ 只股票全部存在本地 SQLite `stock_xxx` 表，每只 1500+ 行
- `main.py train` 走 `ml.ranker.prepare_training_data` → `load_stock_daily`（纯本地 SQLite，不联网）
- 但 `main.py evolve` 走的是 `ml.auto_evolve.evolve`，里面有早期遗留的 baostock 拉数代码 → **今日 evolve 失败的根因**

## 当前 BaoStock 残留盘点

| 文件 | 行数 | 状态 |
|------|------|------|
| `ml/auto_evolve.py` | L78, L110 | 🔴 生产链路用，今日 evolve 失败元凶 |
| `data/bulk_fetcher.py` | 全文件 | 🟠 fetch-all 不带 `--tushare` 时走（已无人这么用） |
| `data/fetcher.py:300-334` `fetch_daily_baostock` | 35 行 | 🟠 `fetch_etf_daily` 三级降级末端 |
| `scripts/validate_data.py` | 全文件 | 🟢 仅手动数据校验脚本（已废弃） |
| `requirements.txt` | 1 行 | 🟡 依赖声明 |

---

## Phase 1：重写 `ml/auto_evolve.py:evolve` —— 走本地 SQLite，对齐 train（约 30 分钟）

### 现状（problematic）

`ml/auto_evolve.py:74-180` 自己重新搭了一套数据流：
```python
# Step 2 股票池：先 get_small_cap_stocks，失败时 fallback 到 baostock 拉全市场
# Step 3 滚动因子：固定调 baostock query_history_k_data_plus 拉日线
#                   限制 200 只，基本面因子全填 NaN
```

完全绕开本地 SQLite + 标准 `prepare_training_data`，云主机 baostock 不通就 0 样本。

### 目标改造

让 `evolve` 复用 `main.py train` 完全相同的训练入口：
1. `get_small_cap_stocks()` 失败时**用本地 SQLite 抽样**，不再 baostock fallback
2. 因子计算用 `factors.calculator.compute_stock_pool_factors`（已是纯本地）
3. 训练数据生成用 `ml.ranker.prepare_training_data`（已是纯本地）
4. 训练用 `ml.ranker.train_model`（不变）

### 改动详情

**`ml/auto_evolve.py:evolve()` 整体重写**，目标结构：

```python
def evolve(push: bool = False) -> dict:
    """
    模型自动进化 — 纯本地数据路径

    流程: 读旧模型 R² → 计算因子(本地SQLite) → 准备训练数据(滚动截面)
        → 训练新模型(自动版本管理) → 推送报告
    """
    report = {
        "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "steps": {},
        "decision": None,
    }
    print("=" * 60)
    print("模型自动进化")
    print("=" * 60)

    # === Step 1: 旧模型基准 ===
    print("\n[1/4] 获取旧模型基准...")
    old_info = get_model_info()
    old_r2 = old_info.get("current", {}).get("cv_r2_mean")
    print(f"  当前模型 R²: {old_r2}")
    report["steps"]["old_model"] = {
        "old_r2": old_r2,
        "version_count": old_info.get("version_count", 0),
    }

    # === Step 2: 计算因子（含情绪） ===
    print("\n[2/4] 计算因子（含情绪）...")
    from factors.calculator import compute_stock_pool_factors
    factor_df = compute_stock_pool_factors(skip_sentiment=False)

    if factor_df.empty:
        report["decision"] = "ABORT: 因子计算失败 / 股票池为空"
        return _finish_report(report, push)

    pool_size = len(factor_df)
    has_sent = int((factor_df.get("sentiment_score", 0).fillna(0) != 0).sum())
    print(f"  股票池: {pool_size} 只")
    print(f"  情绪覆盖: {has_sent}/{pool_size}")
    report["steps"]["stock_pool"] = {"count": pool_size}
    report["steps"]["factors"] = {"sentiment_coverage": f"{has_sent}/{pool_size}"}

    if pool_size < 20:
        report["decision"] = f"ABORT: 股票池不足 20 只 ({pool_size})"
        return _finish_report(report, push)

    # === Step 3: 准备训练数据（滚动截面，纯本地） ===
    print("\n[3/4] 准备训练数据（滚动截面）...")
    from ml.ranker import prepare_training_data
    train_df = prepare_training_data(factor_df)

    if train_df.empty or len(train_df) < 50:
        report["decision"] = f"ABORT: 训练样本不足 ({len(train_df)} < 50)"
        report["steps"]["factors"]["train_samples"] = len(train_df)
        return _finish_report(report, push)

    # 注入情绪因子（截面均值，与 ranker.predict 一致）
    if "sentiment_score" not in train_df.columns:
        sent_map = dict(zip(factor_df["code"], factor_df.get("sentiment_score", 0)))
        train_df["sentiment_score"] = train_df["code"].map(sent_map).fillna(0)

    print(f"  训练样本: {len(train_df)} 条")
    report["steps"]["factors"]["train_samples"] = len(train_df)

    # === Step 4: 训练新模型（自动版本管理） ===
    print("\n[4/4] 训练新模型...")
    result = train_model(train_df)

    if not result:
        report["decision"] = "ABORT: 训练失败"
        return _finish_report(report, push)

    new_r2 = result["cv_r2_mean"]
    is_best = result.get("is_new_best", True)

    report["steps"]["training"] = {
        "new_r2": new_r2,
        "new_r2_std": result["cv_r2_std"],
        "train_samples": result["train_samples"],
        "is_new_best": is_best,
        "old_r2": old_r2,
        "top_factors": list(result.get("feature_importance", {}).keys())[:5],
    }

    if is_best:
        report["decision"] = f"✓ 上线新模型 (R² {old_r2}→{new_r2})"
    else:
        report["decision"] = f"⚠ 保留旧模型 (新 R²={new_r2} < 旧 {old_r2})"

    return _finish_report(report, push)
```

### 删除项
- `import baostock as bs` 全部删（文件顶部 + Step 2 fallback 段 + Step 3 整段）
- 删 `bs.login() / bs.logout()` / `query_stock_basic` / `query_history_k_data_plus`
- 删 200 只限制
- 删 `for col in ["pe_ttm", "pb", "turnover_rate", "volume_ratio"]: factors[col] = np.nan` 这段（用 prepare_training_data 标准路径）
- 删局部的 `_batch_sentiment_factors` 二次调用（compute_stock_pool_factors 内部已有）

### Commit message
```
fix(evolve): 重写 auto_evolve.evolve 走本地 SQLite + prepare_training_data

根因: 2026-05-01 evolve 推送 ABORT 训练样本不足 (0 < 50)
原因: auto_evolve.evolve 自己重写一套数据流，固定调 baostock 拉日线，
     云主机 baostock 失败 → 200 只股票全部 except → 0 样本 → ABORT

修复:
- 删除 evolve() 内全部 baostock 代码（股票池 fallback + 滚动因子）
- 改为复用 main.py train 的标准入口：
  compute_stock_pool_factors → prepare_training_data → train_model
- 全程纯本地 SQLite，不依赖 baostock 网络可用性
- 流程从 5 步精简为 4 步（旧模型基准 → 因子 → 训练数据 → 训练）

效果: 只要本地 SQLite 有数据（即使 baostock/tushare 都不通）也能训出模型。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## Phase 2：清理三个废弃 baostock 文件（约 15 分钟）

### 2.1 删除 `data/bulk_fetcher.py`

- 已被 `data/tushare_daily.py` 替代（按日期批量比按股串行快 20x）
- `main.py:359-361` `fetch-all` 命令的 baostock 分支也要删

**`main.py` 修改**：

```python
# 修改前 (L355-361)
if "--tushare" in sys.argv:
    from data.tushare_daily import run as tushare_daily_run
    _incremental = "--incremental" in sys.argv
    tushare_daily_run(limit=_limit, incremental=_incremental)
else:
    from data.bulk_fetcher import bulk_fetch
    _refresh = "--refresh" in sys.argv
    bulk_fetch(limit=_limit, refresh=_refresh)

# 修改后：fetch-all 默认走 tushare（不再支持 --tushare 开关，去掉 baostock 分支）
from data.tushare_daily import run as tushare_daily_run
_incremental = "--incremental" in sys.argv
tushare_daily_run(limit=_limit, incremental=_incremental)
```

**`run_daily.sh` 修改**（去掉 `--tushare` 已无意义）：

```bash
# 旧
python3 main.py fetch-all --tushare --incremental

# 新（保留 --tushare 兼容性也行；建议直接删使命令更干净）
python3 main.py fetch-all --incremental
```

注意：保留 `--tushare` 入参做向后兼容也可以，但 main.py 内部不再据此分支。

### 2.2 删除 `scripts/validate_data.py`

baostock-only 校验脚本，已被 `data/tushare_fundamentals.py` 内部 `_verify_import()` 取代。

```bash
rm scripts/validate_data.py
```

### 2.3 删除 `data/fetcher.py:fetch_daily_baostock` + `fetch_etf_daily` 的 baostock 降级

```python
# fetcher.py:300-334 整个 fetch_daily_baostock 函数删除

# fetcher.py:367-374 fetch_etf_daily 内 baostock 降级段删除
def fetch_etf_daily(symbol: str, start_date: str, end_date: str = None):
    """获取日线数据 — 多数据源自动降级，优先级: 东方财富 → AKShare"""
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    try:
        df = fetch_daily_eastmoney(symbol, start_date, end_date)
        if len(df) > 0:
            return df
    except Exception as e:
        logger.warning(f"[东方财富] {symbol} 失败: {e}")

    try:
        df = fetch_daily_akshare(symbol, start_date, end_date)
        if len(df) > 0:
            return df
    except Exception as e:
        logger.warning(f"[AKShare] {symbol} 失败: {e}")

    raise RuntimeError(f"所有数据源均获取失败: {symbol}")
```

ETF 数据腾讯/东方财富免费且稳定，AKShare 兜底足够。

### Commit message
```
chore: 移除三处废弃 baostock 数据获取代码

- 删除 data/bulk_fetcher.py（已被 tushare_daily.py 替代）
- 删除 scripts/validate_data.py（baostock-only 校验脚本，已废弃）
- 删除 data/fetcher.py:fetch_daily_baostock + ETF 降级链 baostock 段
- main.py fetch-all 命令去掉 baostock 分支，统一走 tushare
- run_daily.sh fetch-all 命令同步简化

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## Phase 3：删除 baostock 依赖（约 5 分钟）

### 3.1 `requirements.txt` 移除 baostock

```bash
# 修改前
baostock==0.8.9
# 修改后：直接删该行
```

### 3.2 全 repo grep 复查

```bash
grep -rn "baostock\|bs\.login\|query_history_k_data_plus" \
    --include="*.py" --include="*.sh" --include="*.md" \
    --include="requirements.txt" /Users/wei/pj_quant
# 预期: 无输出，或仅在历史 PROGRESS.md / FIX_PROMPT*.md 中出现
```

### 3.3 验证 import 链不破

```bash
python3 -c "
from data.fetcher import fetch_etf_daily, fetch_realtime_tencent_batch
from data.tushare_daily import run as tushare_run
from data.tushare_fundamentals import run as fund_run
from ml.auto_evolve import evolve
from ml.ranker import train_model, predict, prepare_training_data
from factors.calculator import compute_stock_pool_factors
print('All imports OK')
"
```

### Commit message
```
chore: requirements.txt 移除 baostock 依赖

完成数据源迁移到 Tushare：
- K 线: data/tushare_daily.py
- 基本面: data/tushare_fundamentals.py
- 实时: data/fetcher.py 腾讯/东方财富/新浪
- ETF: data/fetcher.py 东方财富 → AKShare 降级

不再保留 baostock 兜底。

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 整体验收清单

### Phase 1 验收

```bash
# 1. evolve 烟囱测试（不需联网，纯本地）
python3 -c "
from ml.auto_evolve import evolve
# 不要真训，只看函数签名和 import 是否 OK
import inspect
print(inspect.signature(evolve))
"

# 2. 真实跑一次 evolve（约 5-10 分钟）
python3 main.py evolve
# 预期: 不应再出现 ABORT 训练样本不足 (前提是本地有 SQLite 数据)

# 3. evolve 历史
python3 main.py evolve-history
# 预期: 至少有一条新版本记录
```

### Phase 2 验收

```bash
# 1. 文件已删除
ls data/bulk_fetcher.py 2>&1   # No such file
ls scripts/validate_data.py 2>&1   # No such file

# 2. fetcher.py 内 baostock 函数已删
grep -n "baostock" data/fetcher.py   # 无输出

# 3. fetch-all 命令仍可用
python3 main.py fetch-all --limit 5 --incremental
# 预期: 走 tushare_daily 路径
```

### Phase 3 验收

```bash
# 1. requirements.txt 干净
grep "baostock" requirements.txt   # 无输出

# 2. 全 repo 残留检查
grep -rn "import baostock\|from baostock\|bs\.login" --include="*.py" .
# 预期: 无输出（历史 md 文件不算）

# 3. 测试通过
python3 -m pytest tests/ -q
# 预期: 全部通过（应保持原项数）
```

### 整体验收

```bash
# 1. evolve 在云主机能跑出非 0 样本
ssh 云主机 "cd /home/ubuntu/pj_quant && git pull origin feature/simulated-trading && \
    python3 main.py evolve"

# 2. 推送报告中训练样本应 ≥ 1000
# 关键字段：训练样本: N 条 (N ≥ 1000)
```

---

## 风险与回退

| 风险 | 缓解 |
|------|------|
| Phase 1 重写后 evolve 实际跑不出预期样本数 | `prepare_training_data` 是 main 已用了几个月的成熟代码，本身稳定；先在本机跑过再推 |
| Phase 2 删除 bulk_fetcher 后某条历史脚本/cron 还在调用 | 已 grep 全 repo，run_daily.sh 是唯一入口且已改 tushare |
| Phase 3 venv 里 baostock 包还在但代码不 import | 不影响功能，下次 `pip install -r requirements.txt --no-deps` 时会清理 |
| auto_evolve 删的 sentiment 局部计算会让覆盖率显示不一致 | 用 factor_df 的 sentiment_score 作为权威来源（compute_stock_pool_factors 内部已算） |

回退：每个 Phase 单独 commit，任何一步出问题 git revert 即可。

---

## 提交规范

按 3 个 commit 拆分，顺序为 Phase 1 → Phase 2 → Phase 3。建议在本机先跑通 Phase 1 的 evolve 烟囱测试再推送。

每个 commit 末尾：
```
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 部署步骤（修复完成后云主机）

```bash
# 云主机
ssh 云主机
cd /home/ubuntu/pj_quant
git pull origin feature/simulated-trading

# 立即手动跑一次 evolve 验证
python3 main.py evolve --push
# 预期: 推送报告中训练样本数 > 1000，cv_r2_mean > 0.05

# 下个月 1 号 16:00 cron 自动跑，无需再干预
```
