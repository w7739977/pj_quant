# 修复 Prompt — auto_evolve / fetcher 重构后清尾

> 分支：`feature/simulated-trading`
> 基于 commit `fb04b85` 的 baostock 移除三件套
> 工程量约 5 分钟，单 commit 完成

---

## 背景

baostock 移除已闭环（commit 66b47bb / 6df6a85 / fb04b85），但 review 发现 4 个细节尾巴需要扫尾。

---

## Issue 1：`data/fetcher.py:9` 注释残留 BaoStock

文件顶部 docstring 列了 5 级数据源，第 5 级 BaoStock 已删除但注释未同步。

```python
# 文件顶部 docstring
数据源优先级:
1. 本地 SQLite 缓存
2. 东方财富直连 API (HTTP JSON)
3. AKShare (pip 包)
4. 腾讯财经 (HTTP)
5. BaoStock (TCP)        # ← 删此行
```

**修复**：删除 `5. BaoStock (TCP)` 那一行。

---

## Issue 2（最重要）：`ml/auto_evolve.py:184-189` 推送 emoji 判断永远命中 else 分支

### 现状

```python
def _push_report(report: dict):
    decision = report["decision"]
    ...
    if "NEW_MODEL_DEPLOYED" in decision:
        emoji = "✓ 新模型已上线"
    elif "OLD_MODEL_RETAINED" in decision:
        emoji = "→ 保留旧模型"
    else:
        emoji = f"✗ {decision}"
```

但 `evolve()` 主函数 L140-143 实际写的 decision 字符串是：
```python
report["decision"] = f"✓ 上线新模型 (R² {old_r2}→{new_r2})"     # 不含 NEW_MODEL_DEPLOYED
report["decision"] = f"⚠ 保留旧模型 (新 R²={new_r2} < 旧 {old_r2})"  # 不含 OLD_MODEL_RETAINED
```

**结果**：成功 / 保留两种正常路径都掉进 else，推送标题变成 `✗ ✓ 上线新模型 (R²...)` —— 三种正反符号混杂。

### 修复

```python
def _push_report(report: dict):
    """微信推送进化报告"""
    try:
        from alert.notify import send_to_all
    except ImportError:
        print("推送模块不可用，跳过")
        return

    decision = report["decision"] or ""
    steps = report["steps"]

    # 按 decision 首字符判断（与 evolve() 主函数 L140-143 输出对齐）
    if decision.startswith("✓"):
        emoji = "✓ 新模型已上线"
    elif decision.startswith("⚠"):
        emoji = "→ 保留旧模型"
    else:
        emoji = f"✗ {decision}"

    training = steps.get("training", {})
    new_r2 = training.get("new_r2", "N/A")
    old_r2 = training.get("old_r2", "N/A")
    samples = training.get("train_samples", "N/A")
    factors = steps.get("factors", {})

    title = f"模型进化报告 ({emoji})"
    msg = f"""**模型进化报告**
时间: {report.get('start_time', '')}

**决策: {emoji}**

模型对比:
- 旧模型 R²: {old_r2}
- 新模型 R²: {new_r2}
- 训练样本: {samples}

数据概况:
- 股票池: {steps.get('stock_pool', {}).get('count', 'N/A')} 只
- 训练样本: {factors.get('train_samples', 'N/A')} 条
- 情绪覆盖: {factors.get('sentiment_coverage', 'N/A')}

Top 5 因子:
{chr(10).join(f'  {i+1}. {f}: {v:.4f}' for i, (f, v) in enumerate(factors.get('top5', [])))}"""

    send_to_all(title, msg)
    print("进化报告已推送到微信")
```

---

## Issue 3：`ml/auto_evolve.py:43` 多余 import

```python
from config.settings import INITIAL_CAPITAL  # ← evolve() 内未使用，删此行
```

`evolve()` 重写后没引用 `INITIAL_CAPITAL`，dead import。

**修复**：删除 L43 该行（连带相邻空行整理）。

---

## Issue 4：`ml/auto_evolve.py:99-102` 情绪因子注入死代码

### 现状

```python
# 注入情绪因子（截面均值，与 ranker.predict 一致）
if "sentiment_score" not in train_df.columns:
    sent_map = dict(zip(factor_df["code"], factor_df.get("sentiment_score", 0)))
    train_df["sentiment_score"] = train_df["code"].map(sent_map).fillna(0)
```

但 `ml/ranker.py:prepare_training_data:L109` 始终主动写 `factors["sentiment_score"] = np.nan`，所以 `train_df` **总有该列**，整段判断永远为 False，从不执行。

### 修复（推荐 B —— 让情绪因子真正起作用）

```python
# 用最新截面情绪覆盖训练数据的 NaN
# (prepare_training_data 内部仅写 NaN 占位，这里用 factor_df 的实际值替换，
#  保持训练特征与 predict 时口径一致)
if "sentiment_score" in train_df.columns and train_df["sentiment_score"].isna().all():
    sent_map = dict(zip(factor_df["code"], factor_df.get("sentiment_score", 0)))
    train_df["sentiment_score"] = train_df["code"].map(sent_map).fillna(0)
```

替换原 L99-102 即可。

---

## 验收

```bash
# 1. 注释清理
grep -n "BaoStock\|baostock" data/fetcher.py
# 预期: 无输出

# 2. dead import 检查
grep -n "INITIAL_CAPITAL" ml/auto_evolve.py
# 预期: 无输出

# 3. 推送 emoji 判断逻辑测试
python3 -c "
import sys
sys.path.insert(0, '.')
# mock send_to_all
import alert.notify
sent = []
alert.notify.send_to_all = lambda t, m: sent.append((t, m))

from ml.auto_evolve import _push_report

# 模拟 3 种 decision
for dec in ['✓ 上线新模型 (R² 0.05→0.08)',
            '⚠ 保留旧模型 (新 R²=0.04 < 旧 0.07)',
            'ABORT: 训练样本不足 (0 < 50)']:
    _push_report({
        'decision': dec, 'start_time': '2026-05-01',
        'steps': {'training': {}, 'factors': {}, 'stock_pool': {}},
    })

for title, _ in sent:
    print(title)
"
# 预期输出（无 ✗✓ 这种叠加）：
# 模型进化报告 (✓ 新模型已上线)
# 模型进化报告 (→ 保留旧模型)
# 模型进化报告 (✗ ABORT: 训练样本不足 (0 < 50))

# 4. 测试通过
python3 -m pytest tests/ -q
# 预期: 57 项全过
```

---

## 提交

单 commit：

```
chore(evolve): 清理 baostock 移除后的 4 个尾巴

Issue 1: data/fetcher.py docstring 删除残留的 "5. BaoStock (TCP)" 行
Issue 2: auto_evolve._push_report emoji 判断改用 decision 首字符匹配
         (原代码用 NEW_MODEL_DEPLOYED/OLD_MODEL_RETAINED 字符串永远不命中)
Issue 3: auto_evolve.py 删除未使用的 INITIAL_CAPITAL import
Issue 4: 情绪因子注入逻辑改为"列存在且全 NaN 时用 factor_df 覆盖"
         让情绪因子在训练中真正起作用（之前条件永远 False 等于死代码）

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 风险

零风险。Issue 1 是注释，Issue 3 是 dead import，Issue 4 把死代码激活但行为只在情绪因子覆盖率高时才有差异。Issue 2 修复后推送标题更整洁。
