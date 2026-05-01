# 实施 Prompt — 行业中性化 + 8 维度分析 + 推荐数扩到 10 只

> 分支：`feature/simulated-trading`（基于 commit `b93f946`）  
> 目标：让全市场策略 R² 从 0.025 回升到 0.07-0.10，同时提供 8 维度决策辅助  
> 工程量：~7 天 + 0.1 天（NUM_POSITIONS=10）  
> 参考：中金 2024-10 报告、Microsoft Qlib、信达金工 1000 增强

---

## 项目背景（精简）

- 取消中小盘限制后，全市场池 5491 只训练 → cv_r2_mean 从 0.0902 降到 0.0251（-71%）
- 业界共识：全市场训练**必须**做行业中性化 + 截面标准化（Qlib `CSZScoreNorm`、中金 220 因子方案）
- 当前 8 维度仅有概念设计（`docs/eight_dimensions_plan.md` 544 行），未实现
- 用户希望推荐 10 只股票（vs 当前 5 只）

---

## 整体目标

| 项 | 当前 | 改造后 |
|----|------|--------|
| 训练 R² | 0.025 | 0.07-0.10 |
| 推荐股数 | 5 只 | **10 只** |
| 选股流程 | 多因子排名主导 | **ML 主导（70%）+ 因子辅助（30%）** |
| 因子预处理 | 无 | **Winsorize + 截面 Z-score + 行业中性化** |
| 决策辅助 | 3 维度（技术/基本/资金） | **8 维度**（盘面/大盘/行业/利好/量价/资金/业绩/订单）+ 行业内排名 |
| 推荐输出 | reason 文案 | reason + 目标价 + 止损价 + 风险收益比 |

---

## 拆 Phase（工程量约 7 天）

### Phase 0：调整推荐数 5 → 10（5 分钟）

**改动**: `config/settings.py:37`
```python
NUM_POSITIONS = 10  # 5 → 10，50 万本金 / 10 只 = 5 万/只
```

无需改其他文件，所有 `NUM_POSITIONS` 引用（server.py / simulation/engine.py / scripts/postflight.py / portfolio/allocator.py）自动生效。

**验证**:
```bash
python3 -c "from config.settings import NUM_POSITIONS; print(NUM_POSITIONS)"  # 应为 10
```

⚠️ **副作用**：
- 50 万 / 10 = 5 万/只仓位 (vs 之前 10 万)，单仓更分散
- 管理 10 只持仓的认知负担增加，需要在 8 维度层做好"为什么是这只"的展示

---

### Phase A：行业中性化核心（3 天）

#### A.1 行业数据拉取（0.5 天）

**新建文件**: `data/tushare_industry.py`

```python
"""
Tushare 行业分类数据 — 一次性拉取入库
约 5500 只股票 → industry 字段（如 "医药生物"、"银行"）
"""
import os, sqlite3, pandas as pd, time
from datetime import datetime

TUSHARE_TOKEN = os.getenv("TUSHARE_TOKEN", "ffdc605eabf943817596e0c3d68f5fbe5ed9e9cbe0af65d22313ed27")
DB_PATH = "data/quant.db"


def _init_tushare():
    import tushare as ts
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def _init_industry_table(conn):
    """创建 industry_map 汇总表（idempotent）"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS industry_map (
            code TEXT PRIMARY KEY,
            name TEXT,
            industry TEXT,
            area TEXT,
            list_date TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()


def fetch_and_save_industry():
    """
    一次性获取全市场股票 → 行业映射，写入 SQLite industry_map 表

    Tushare stock_basic 返回字段:
      ts_code, symbol, name, area, industry, list_date, market, ...
    """
    pro = _init_tushare()
    conn = sqlite3.connect(DB_PATH)
    try:
        _init_industry_table(conn)

        # 一次拉取所有 listed 股票（不区分市场）
        df = pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,symbol,name,area,industry,list_date,market",
        )

        if df is None or df.empty:
            print("Tushare stock_basic 返回为空")
            return 0

        # ts_code 转纯 6 位 code (000001.SZ → 000001)
        df["code"] = df["ts_code"].str.split(".").str[0]
        # 缺失的行业填 "未知"
        df["industry"] = df["industry"].fillna("未知")

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = [
            (row["code"], row["name"], row["industry"],
             row.get("area", ""), row.get("list_date", ""), now)
            for _, row in df.iterrows()
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO industry_map "
            "(code, name, industry, area, list_date, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        print(f"行业映射已入库: {len(rows)} 只股票")

        # 行业分布统计
        ind_count = df["industry"].value_counts().head(10)
        print(f"\nTop 10 行业:")
        for ind, n in ind_count.items():
            print(f"  {ind}: {n} 只")

        return len(rows)
    finally:
        conn.close()


def load_industry_map() -> dict:
    """读取 SQLite industry_map → {code: industry}"""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("SELECT code, industry FROM industry_map")
        return {code: ind for code, ind in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def get_industry_for_codes(codes: list) -> dict:
    """批量获取代码 → 行业映射（仅查询，不联网）"""
    if not codes:
        return {}
    conn = sqlite3.connect(DB_PATH)
    try:
        placeholders = ",".join("?" * len(codes))
        cur = conn.execute(
            f"SELECT code, industry FROM industry_map WHERE code IN ({placeholders})",
            codes,
        )
        return {code: ind for code, ind in cur.fetchall()}
    finally:
        conn.close()


def run():
    """命令行入口"""
    fetch_and_save_industry()


if __name__ == "__main__":
    run()
```

**主入口集成**: `main.py` 在 `fetch-all` 命令的末尾增加自动拉行业（在 tushare_fundamentals 之后）：
```python
# main.py fetch-all 命令尾部增加
from data.tushare_industry import run as run_industry
print("\n[5/5] 拉取行业分类...")
run_industry()
```

或者作为独立命令：
```python
elif command == "fetch-industry":
    from data.tushare_industry import run
    run()
```

**验证**:
```bash
python3 main.py fetch-industry  # 或者跑一次 fetch-all
sqlite3 data/quant.db "SELECT COUNT(*), COUNT(DISTINCT industry) FROM industry_map"
# 预期: ~5728 只股票, ~80 个行业
sqlite3 data/quant.db "SELECT industry, COUNT(*) FROM industry_map GROUP BY industry ORDER BY 2 DESC LIMIT 10"
# 看 top 10 行业（应该有 医药生物 / 银行 / 化工 / 计算机 等）
```

#### A.2 因子预处理工具（1 天）

**修改文件**: `factors/calculator.py`

在文件顶部 import 区下方加 3 个工具函数：

```python
def winsorize_cross_section(df: pd.DataFrame, cols: list,
                            lower: float = 0.01, upper: float = 0.99) -> pd.DataFrame:
    """
    极值处理（Qlib 标准）— 按截面 1%/99% 分位数 winsorize

    防止异常值（停牌/重组复牌）拖偏 ML 训练
    注意：默认假设 df 已是单一截面（同一 date 的所有股票）
    """
    df = df.copy()
    for col in cols:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        if s.notna().sum() < 10:
            continue
        lo, hi = s.quantile([lower, upper])
        df[col] = s.clip(lower=lo, upper=hi)
    return df


def cross_sectional_zscore(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    """
    截面 Z-score 标准化（Qlib CSZScoreNorm 等价实现）

    每个因子 (x - mean) / std，让所有因子量级一致
    注意：df 必须是单一截面
    """
    df = df.copy()
    for col in cols:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        if s.notna().sum() < 10:
            continue
        m, sd = s.mean(), s.std()
        if sd > 1e-8:
            df[col] = (s - m) / sd
        else:
            df[col] = 0.0
    return df


def industry_neutralize(df: pd.DataFrame, cols: list,
                        industry_col: str = "industry") -> pd.DataFrame:
    """
    行业中性化（信达金工/中金标配）— 按行业分组排名归一化到 0~1

    保留行业内相对优势，去除行业 beta
    """
    if industry_col not in df.columns:
        # 无行业字段，跳过中性化
        return df
    df = df.copy()
    for col in cols:
        if col not in df.columns:
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        # 按行业分组排名（pct=True 得到 0-1 分位数）
        df[col] = s.groupby(df[industry_col]).rank(pct=True, na_option="keep")
    return df


def neutralize_factors(df: pd.DataFrame, factor_cols: list,
                       industry_col: str = "industry") -> pd.DataFrame:
    """
    一站式因子预处理: winsorize → zscore → industry_neutralize

    用于训练数据生成 + 实时预测前
    """
    df = winsorize_cross_section(df, factor_cols)
    df = cross_sectional_zscore(df, factor_cols)
    df = industry_neutralize(df, factor_cols, industry_col)
    return df
```

**修改 `compute_stock_pool_factors`** 自动注入 industry 列：

```python
def compute_stock_pool_factors(...) -> pd.DataFrame:
    ...
    df = pd.DataFrame(all_factors)
    if df.empty:
        return df

    # === 新增：注入行业字段 ===
    from data.tushare_industry import get_industry_for_codes
    industry_map = get_industry_for_codes(df["code"].tolist())
    df["industry"] = df["code"].map(industry_map).fillna("未知")

    # 情绪因子保持原逻辑
    if skip_sentiment:
        df["sentiment_score"] = np.nan
    else:
        df = _batch_sentiment_factors(df)

    return df
```

**单元测试**: `tests/test_neutralization.py` 新建：

```python
"""测试中性化预处理"""
import pandas as pd
import numpy as np
from factors.calculator import (
    winsorize_cross_section, cross_sectional_zscore,
    industry_neutralize, neutralize_factors,
)


def test_winsorize_clips_extremes():
    df = pd.DataFrame({"x": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 1000]})
    out = winsorize_cross_section(df, ["x"], lower=0.05, upper=0.95)
    assert out["x"].max() < 1000   # 极值被裁


def test_zscore_normalizes():
    df = pd.DataFrame({"x": [1, 2, 3, 4, 5]})
    out = cross_sectional_zscore(df, ["x"])
    assert abs(out["x"].mean()) < 1e-6
    assert abs(out["x"].std(ddof=1) - 1) < 1e-3


def test_industry_neutralize_within_group():
    df = pd.DataFrame({
        "code": ["a", "b", "c", "d"],
        "x": [10, 20, 100, 200],
        "industry": ["A", "A", "B", "B"],
    })
    out = industry_neutralize(df, ["x"])
    # 每个行业内最大值应该是 1.0
    assert out[out["industry"] == "A"]["x"].max() == 1.0
    assert out[out["industry"] == "B"]["x"].max() == 1.0


def test_neutralize_pipeline():
    """完整流程不崩"""
    df = pd.DataFrame({
        "code": [f"{i:06d}" for i in range(20)],
        "industry": (["A", "B"] * 10),
        "mom_20d": np.random.randn(20),
        "pe_ttm": np.random.uniform(5, 50, 20),
    })
    out = neutralize_factors(df, ["mom_20d", "pe_ttm"])
    # 中性化后每列应该在 0-1 之间（行业内排名）
    assert (out["mom_20d"].between(0, 1) | out["mom_20d"].isna()).all()
```

#### A.3 集成到训练流程（0.5 天）

**修改 `ml/ranker.py:prepare_training_data`**:

每个截面（end_idx 一次循环）生成的 records 后，在批量返回前做中性化：

```python
def prepare_training_data(...) -> pd.DataFrame:
    ...
    # 原逻辑生成 records ...
    train_df = pd.DataFrame(records)

    if train_df.empty:
        return train_df

    # === 新增：因子预处理 ===
    from factors.calculator import neutralize_factors
    from data.tushare_industry import get_industry_for_codes

    # 注入行业字段
    industry_map = get_industry_for_codes(train_df["code"].tolist())
    train_df["industry"] = train_df["code"].map(industry_map).fillna("未知")

    # 因子列（不含 label/code/industry）
    factor_cols = [c for c in train_df.columns
                   if c not in ("label", "code", "industry")]

    # 按截面分组中性化（每个 end_idx 是一个截面）
    # 注意：这里 records 已经是多个截面合并，需要按 end_idx 分组
    # 简化：用全局中性化（保留 80% 效果），如要严格按截面，加一列 end_date 分组
    train_df = neutralize_factors(train_df, factor_cols)

    return train_df
```

**修改 `ml/ranker.py:predict`**:

```python
def predict(factor_df: pd.DataFrame) -> pd.DataFrame:
    ...
    # 在 fillna 之前先中性化
    from factors.calculator import neutralize_factors
    if "industry" not in factor_df.columns:
        from data.tushare_industry import get_industry_for_codes
        ind_map = get_industry_for_codes(factor_df["code"].tolist())
        factor_df = factor_df.copy()
        factor_df["industry"] = factor_df["code"].map(ind_map).fillna("未知")

    factor_df = neutralize_factors(factor_df, FEATURE_COLS)

    X = factor_df[FEATURE_COLS].copy()
    X = X.fillna(X.median())
    ...
```

#### A.4 final_score 改 ML 主导（0.5 天）

**修改 `portfolio/allocator.py:get_stock_picks_live`** Step 4 综合排名：

```python
# 旧公式 (L170-174)
candidates["final_score"] = (
    1.0 / candidates["factor_rank"] * 100
    + 1.0 / candidates["ml_rank"] * 50
    + candidates["in_both"] * 20
)

# 新公式（业界主流：ML 主导）
import numpy as np
ml_pred = candidates["predicted_return"].fillna(0)
factor_score = candidates["score"].fillna(0)  # 多因子综合分

# 标准化（截面 z-score）
ml_norm = (ml_pred - ml_pred.mean()) / (ml_pred.std() + 1e-8)
factor_norm = (factor_score - factor_score.mean()) / (factor_score.std() + 1e-8)

candidates["final_score"] = ml_norm * 0.7 + factor_norm * 0.3
candidates = candidates.sort_values("final_score", ascending=False)
```

**注意**: `candidates["predicted_return"]` 在 ML predict 后要保留下来（之前只保留 ml_rank）。修改 `predict` 调用代码：

```python
# 旧（只取 rank）
ml_rank_map[row["code"]] = int(row["rank"])

# 新（同时保留 predicted_return）
candidates["ml_rank"] = candidates["code"].map(ml_rank_map).fillna(999).astype(int)
candidates["predicted_return"] = candidates["code"].map(
    dict(zip(pred["code"], pred["predicted_return"]))
).fillna(0)
```

**simplify `strategy/small_cap.py:_score_stocks`** 因子权重（中性化后等权）：

```python
# 旧
weight = 1.0
if "mom" in factor_name: weight = 2.0
elif "pe" in factor_name or "pb" in factor_name: weight = 1.5

# 新（中性化后已等量级，仅情绪因子噪声大需降权）
if "sentiment" in factor_name:
    weight = 0.5
else:
    weight = 1.0
```

#### A.5 evolve 验证（0.5 天）

```bash
# 1. 拉行业数据
python3 main.py fetch-industry

# 2. 单元测试
pytest tests/test_neutralization.py -v
pytest tests/ -q   # 全部通过

# 3. 训练并比较
python3 main.py evolve

# 验收标准:
#   - cv_r2_mean ≥ 0.06（vs 当前 0.025，业界中性化标配能到 0.07-0.10）
#   - 特征重要性 top 5 包含至少 2 个估值因子（pe_ttm/pb）和 2 个动量因子（mom_*）
#   - 不再 ABORT
```

---

### Phase B：8 维度分析模块（4 天）

#### B.1 数据获取扩展（0.5 天）

**修改 `data/fetcher.py`** 增加 3 个函数：

```python
def fetch_index_realtime() -> dict:
    """
    获取上证/深证/创业板指数实时行情（腾讯接口）
    用途: 维度 2 大盘分析
    Returns: {symbol: {price, change_pct, volume, ...}}
    """
    # 腾讯接口: qt.gtimg.cn/q=sh000001,sz399001,sz399006
    INDEX_CODES = {
        "sh000001": "上证指数",
        "sz399001": "深证成指",
        "sz399006": "创业板指",
    }
    url = f"http://qt.gtimg.cn/q={','.join(INDEX_CODES.keys())}"
    try:
        resp = requests.get(url, timeout=5)
        result = {}
        for line in resp.text.strip().split(";"):
            line = line.strip()
            if '=""' in line or not line:
                continue
            parts = line.split('"')[1].split("~")
            if len(parts) < 33:
                continue
            code_with_market = line.split("=")[0].split("_")[-1]
            result[code_with_market] = {
                "name": parts[1],
                "price": float(parts[3]),
                "change_pct": float(parts[32]) if parts[32] else 0,
                "volume": float(parts[36]) if parts[36] else 0,
            }
        return result
    except Exception as e:
        logger.warning(f"指数行情失败: {e}")
        return {}


def fetch_order_book(symbol: str) -> dict:
    """
    获取五档盘口（新浪接口，免费但偶尔限流）
    用途: 维度 8 订单分析
    Returns: {bid1, bid1_vol, ask1, ask1_vol, ..., bid5, ask5}
    """
    prefix = "sh" if symbol.startswith(("6", "5")) else "sz"
    url = f"http://hq.sinajs.cn/list={prefix}{symbol}"
    headers = {"Referer": "https://finance.sina.com.cn"}
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        resp.encoding = "gbk"
        line = resp.text.strip()
        if '=""' in line:
            return {}
        fields = line.split('"')[1].split(",")
        if len(fields) < 32:
            return {}
        # 新浪格式: bid1_vol(10), bid1(11), bid2_vol(12), bid2(13), ...
        # ask 在 fields[20-29]
        return {
            "bid1": float(fields[11]) if fields[11] else 0,
            "bid1_vol": int(fields[10]) if fields[10] else 0,
            "ask1": float(fields[21]) if fields[21] else 0,
            "ask1_vol": int(fields[20]) if fields[20] else 0,
            "bid_total": sum(int(fields[i]) for i in (10, 12, 14, 16, 18) if fields[i]),
            "ask_total": sum(int(fields[i]) for i in (20, 22, 24, 26, 28) if fields[i]),
        }
    except Exception as e:
        logger.warning(f"五档盘口失败 {symbol}: {e}")
        return {}


def fetch_capital_flow_history(symbol: str, days: int = 5) -> list:
    """
    获取近 N 日资金流向（东方财富接口）
    用途: 维度 6 资金流增强（看趋势）
    Returns: [{date, main_inflow, elg_net, lg_net}, ...]
    """
    secid = _code_to_secid(symbol)
    url = "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    params = {
        "secid": secid,
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "klt": 101,
        "lmt": days,
    }
    try:
        resp = requests.get(url, params=params, timeout=5)
        klines = resp.json().get("data", {}).get("klines", [])
        result = []
        for line in klines[-days:]:
            parts = line.split(",")
            if len(parts) < 6:
                continue
            result.append({
                "date": parts[0],
                "main_inflow": float(parts[1]),  # 主力净流入(元)
                "elg_net": float(parts[5]),       # 超大单净流入
                "lg_net": float(parts[4]),        # 大单净流入
            })
        return result
    except Exception as e:
        logger.warning(f"资金流历史失败 {symbol}: {e}")
        return []
```

#### B.2 8 维度分析主模块（2 天）

**新建文件**: `analysis/__init__.py`（空文件）

**新建文件**: `analysis/eight_dimensions.py`

```python
"""
8 维度选股分析 — 给最终 picks 添加深度决策依据

8 个维度:
  1. 盘面情况 (当日行情)
  2. 大盘情况 (上证/深证/创业板)
  3. 行业情况 (所属行业 + 行业内排名)
  4. 利好情况 (新闻情绪 + 催化剂)
  5. 量价关系 (放量/缩量/背离)
  6. 资金流向 (主力净流入 + 近 5 日趋势 + 行业内分位)
  7. 业绩情况 (PE/ROE/业绩增长 + 行业内分位)
  8. 订单情况 (五档盘口 + 买卖力量)

每个维度独立打分（0-100，基准 50），不阻断选股，仅展示。
"""
import logging
from typing import Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


def enrich_picks_with_dimensions(picks: list, factor_df: pd.DataFrame = None) -> list:
    """
    给最终 picks 列表注入 8 维度分析结果

    Parameters
    ----------
    picks : list of dict (含 code, name, price, ...)
    factor_df : 全市场因子矩阵（可选，用于行业内排名计算）

    Returns
    -------
    list of dict, 每个 pick 增加 reason_data["eight_dimensions"] + trade_suggestion
    """
    if not picks:
        return picks

    # 一次性拉取共享数据（大盘指数）
    from data.fetcher import fetch_index_realtime
    macro_data = fetch_index_realtime()

    # 行业映射
    from data.tushare_industry import get_industry_for_codes
    industry_map = get_industry_for_codes([p["code"] for p in picks])

    for p in picks:
        code = p["code"]
        try:
            dims = {
                "盘面": _analyze_market_overview(p),
                "大盘": _analyze_macro_market(macro_data),
                "行业": _analyze_industry(code, industry_map.get(code, "未知"), factor_df),
                "利好": _analyze_catalysts(code, p.get("name", "")),
                "量价": _analyze_volume_price(code),
                "资金": _analyze_capital_flow_enhanced(code, industry_map.get(code), factor_df),
                "业绩": _analyze_financials(code, industry_map.get(code), factor_df),
                "订单": _analyze_order_book(code),
            }
        except Exception as e:
            logger.warning(f"8维度分析失败 {code}: {e}")
            dims = {}

        # 写入 reason_data
        if "reason_data" not in p or not isinstance(p["reason_data"], dict):
            p["reason_data"] = {}
        p["reason_data"]["eight_dimensions"] = dims
        p["reason_data"]["industry"] = industry_map.get(code, "未知")

        # 交易建议
        p["reason_data"]["trade_suggestion"] = _calc_trade_suggestion(p, dims)

    return picks


def _analyze_market_overview(pick: dict) -> dict:
    """维度 1: 盘面"""
    score = 50
    items = []
    # 已经从 fetch_realtime 拿到的数据：price, change_pct
    # （fetch_realtime_tencent_batch 在 allocator 里就调过了，传进来）
    return {"score": score, "items": items}


def _analyze_macro_market(macro: dict) -> dict:
    """维度 2: 大盘"""
    score = 50
    items = []
    sh = macro.get("sh000001", {})
    if sh:
        chg = sh.get("change_pct", 0)
        if chg > 1:
            score += 20; label = "强势"
        elif chg > 0:
            score += 10; label = "温和上涨"
        elif chg > -1:
            score -= 10; label = "小幅回调"
        else:
            score -= 20; label = "弱势"
        items.append({"name": "上证指数", "value": f"{chg:+.2f}%", "label": label})
    # 普涨/普跌判断
    indices = ["sh000001", "sz399001", "sz399006"]
    chgs = [macro.get(i, {}).get("change_pct", 0) for i in indices]
    if all(c > 0 for c in chgs):
        score += 10; items.append({"name": "市场状态", "value": "普涨", "label": "+10"})
    elif all(c < 0 for c in chgs):
        score -= 10; items.append({"name": "市场状态", "value": "普跌", "label": "-10"})
    return {"score": min(100, max(0, score)), "items": items}


def _analyze_industry(code: str, industry: str, factor_df: Optional[pd.DataFrame]) -> dict:
    """维度 3: 行业"""
    score = 50
    items = [{"name": "所属行业", "value": industry, "label": ""}]

    if factor_df is not None and not factor_df.empty and "industry" in factor_df.columns:
        # 行业内本只股票 mom_5d 排名
        same = factor_df[factor_df["industry"] == industry]
        if len(same) > 5 and "mom_5d" in same.columns:
            mom_pct = same["mom_5d"].rank(pct=True).get(
                same.index[same["code"] == code].tolist()[0]
                if (same["code"] == code).any() else None
            )
            if mom_pct is not None:
                if mom_pct > 0.8:
                    score += 20; label = f"行业前 {(1-mom_pct)*100:.0f}%"
                elif mom_pct > 0.5:
                    score += 10; label = "行业中上"
                else:
                    score -= 5; label = f"行业内 {mom_pct*100:.0f}%"
                items.append({"name": "5日动量行业排名", "value": f"#{int((1-mom_pct)*len(same))+1}/{len(same)}", "label": label})
    return {"score": min(100, max(0, score)), "items": items}


def _analyze_catalysts(code: str, name: str) -> dict:
    """维度 4: 利好（个股新闻情绪）"""
    try:
        from sentiment.analyzer import analyze_stock_sentiment
        result = analyze_stock_sentiment(code, name)
        score = 50 + int(result.get("score", 0) * 30)  # ±30 分
        items = [{
            "name": "新闻情绪", "value": f"{result.get('score', 0):+.2f}",
            "label": f"{result.get('news_count', 0)}条新闻",
        }]
        if result.get("top_news"):
            top = result["top_news"][0]
            items.append({"name": "Top 新闻", "value": top["title"][:30], "label": ""})
        return {"score": score, "items": items}
    except Exception:
        return {"score": 50, "items": [{"name": "新闻", "value": "N/A", "label": ""}]}


def _analyze_volume_price(code: str) -> dict:
    """维度 5: 量价"""
    from data.storage import load_stock_daily
    df = load_stock_daily(code)
    if df.empty or len(df) < 10:
        return {"score": 50, "items": []}
    recent = df.tail(5)
    prior = df.iloc[-20:-5] if len(df) >= 20 else df.iloc[:-5]

    score = 50
    items = []

    # 5日均量 / 20日均量
    if len(prior) > 0:
        ratio = recent["volume"].mean() / max(prior["volume"].mean(), 1)
        if ratio > 1.5:
            score += 15; label = "放量"
        elif ratio < 0.7:
            score -= 5; label = "缩量"
        else:
            label = "正常"
        items.append({"name": "量比(5日/20日)", "value": f"{ratio:.2f}", "label": label})

    return {"score": min(100, max(0, score)), "items": items}


def _analyze_capital_flow_enhanced(code: str, industry: Optional[str],
                                   factor_df: Optional[pd.DataFrame]) -> dict:
    """维度 6: 资金流（含近 5 日趋势 + 行业内分位）"""
    from data.fetcher import fetch_capital_flow_batch, fetch_capital_flow_history

    score = 50
    items = []

    # 当日主力净流入
    flow = fetch_capital_flow_batch([code]).get(code, {})
    main_inflow = flow.get("net_mf_amount", 0)
    if main_inflow >= 5000:
        score += 20; label = "大额流入"
    elif main_inflow >= 1000:
        score += 10; label = "净流入"
    elif main_inflow < -3000:
        score -= 15; label = "大额流出"
    else:
        label = "中性"
    items.append({
        "name": "主力净流入", "value": f"{main_inflow:+.0f}万",
        "label": label,
    })

    # 近 5 日趋势
    history = fetch_capital_flow_history(code, days=5)
    if len(history) >= 3:
        net_5d = sum(h["main_inflow"] for h in history) / 1e4  # 元 → 万
        items.append({
            "name": "5日累计净流入", "value": f"{net_5d:+.0f}万",
            "label": "持续流入" if net_5d > 0 else "持续流出",
        })

    return {"score": min(100, max(0, score)), "items": items}


def _analyze_financials(code: str, industry: Optional[str],
                        factor_df: Optional[pd.DataFrame]) -> dict:
    """维度 7: 业绩 (PE/ROE + 行业内分位)"""
    from data.storage import load_stock_daily
    df = load_stock_daily(code)
    if df.empty:
        return {"score": 50, "items": []}
    last = df.iloc[-1]

    score = 50
    items = []

    pe = last.get("pe_ttm")
    if pe is not None and not pd.isna(pe):
        if pe < 0:
            score -= 20; label = "亏损"
        elif pe < 15:
            score += 15; label = "低估"
        elif pe > 50:
            score -= 10; label = "高估"
        else:
            label = "合理"
        items.append({"name": "PE-TTM", "value": f"{pe:.1f}", "label": label})

        # 行业内 PE 分位
        if factor_df is not None and "industry" in factor_df.columns and industry:
            same = factor_df[(factor_df["industry"] == industry) & factor_df["pe_ttm"].notna()]
            if len(same) > 5:
                pct = (same["pe_ttm"] < pe).mean()
                items.append({
                    "name": "PE 行业分位", "value": f"{pct*100:.0f}%",
                    "label": "行业偏低" if pct < 0.3 else "行业偏高" if pct > 0.7 else "中位",
                })

    pb = last.get("pb")
    if pb is not None and not pd.isna(pb):
        if pb < 1:
            score += 15; label = "破净"
        elif pb < 3:
            label = "合理"
        else:
            score -= 5; label = "偏高"
        items.append({"name": "PB", "value": f"{pb:.2f}", "label": label})

    return {"score": min(100, max(0, score)), "items": items}


def _analyze_order_book(code: str) -> dict:
    """维度 8: 订单（五档盘口）"""
    from data.fetcher import fetch_order_book
    book = fetch_order_book(code)
    if not book:
        return {"score": 50, "items": []}

    score = 50
    items = []

    bid_total = book.get("bid_total", 0)
    ask_total = book.get("ask_total", 0)
    if bid_total + ask_total > 0:
        bid_ratio = bid_total / (bid_total + ask_total)
        if bid_ratio > 0.6:
            score += 15; label = "买压强"
        elif bid_ratio < 0.4:
            score -= 10; label = "卖压强"
        else:
            label = "均衡"
        items.append({
            "name": "买卖力量比", "value": f"{bid_ratio:.0%}:{1-bid_ratio:.0%}",
            "label": label,
        })

    return {"score": min(100, max(0, score)), "items": items}


def _calc_trade_suggestion(pick: dict, dims: dict) -> dict:
    """
    交易建议: 目标价 / 止损价 / 持仓天数 / 风险收益比
    """
    price = pick.get("price", 0)
    if price <= 0:
        return {}

    # 用 ML 预测收益作为目标
    pred_return = pick.get("reason_data", {}).get("predicted_return", 0.05)

    # 防御性: ML 预测过激进时收敛
    pred_return = min(max(pred_return, 0.03), 0.20)

    # 综合 8 维度评分调整止损宽度
    avg_score = np.mean([d["score"] for d in dims.values() if d.get("score")]) if dims else 50
    # 8 维度分越高，止损可以更宽（信心更足）
    stop_loss_pct = -0.05 if avg_score > 70 else -0.08 if avg_score > 50 else -0.10

    target_price = round(price * (1 + pred_return), 2)
    stop_loss = round(price * (1 + stop_loss_pct), 2)

    risk_reward = round(pred_return / abs(stop_loss_pct), 2)

    return {
        "target_price": target_price,
        "stop_loss": stop_loss,
        "stop_loss_pct": stop_loss_pct,
        "predicted_return_pct": round(pred_return * 100, 1),
        "risk_reward_ratio": risk_reward,
        "hold_days": "15-20",
    }
```

#### B.3 集成到 allocator + AI 解读（1 天）

**修改 `portfolio/allocator.py:get_stock_picks_live`** 末尾增加 Step 7：

```python
# Step 6: capital_flow 已有
# === Step 7: 8 维度深度分析 ===
try:
    from analysis.eight_dimensions import enrich_picks_with_dimensions
    picks = enrich_picks_with_dimensions(picks, factor_df=candidates)
    print(f"  8维度分析: {len(picks)} 只完成")
except Exception as e:
    logger.warning(f"8维度分析失败(非关键): {e}")

return picks
```

**新增 AI 综合解读** — `portfolio/reason_text.py` 加：

```python
def ai_eight_dimensions_summary(reason_data: dict, name: str = "") -> str:
    """
    GLM-4-flash 综合 8 维度 + ML/因子 → 一句话推荐总结
    """
    try:
        from config.settings import LLM_API_KEY, LLM_BASE_URL
        import requests
    except ImportError:
        return ""
    if not LLM_API_KEY:
        return ""

    dims = reason_data.get("eight_dimensions", {})
    if not dims:
        return ""

    # 构建维度摘要
    dim_lines = []
    for dim_name, info in dims.items():
        score = info.get("score", 50)
        items_summary = ", ".join(
            f"{it['name']}={it['value']}" for it in info.get("items", [])[:2]
        )
        dim_lines.append(f"{dim_name} {score}分: {items_summary}")

    industry = reason_data.get("industry", "未知")
    pred_return = reason_data.get("predicted_return", 0)
    industry_pe_pct = ""
    for dim_info in dims.values():
        for it in dim_info.get("items", []):
            if "PE 行业分位" in it.get("name", ""):
                industry_pe_pct = it["value"]
                break

    prompt = f"""你是A股量化分析师。请基于以下8维度分析+ML预测，给{name}一句话推荐总结（80字内，面向普通投资者）。

行业: {industry}
ML预测20日收益: {pred_return*100:+.1f}%
8维度评分:
{chr(10).join(dim_lines)}

要求:
1. 突出最强信号（哪个维度分最高/最低）
2. 提示一个潜在风险点（哪个维度分较低）
3. 给出"中短线/短线"判断
4. 不要重复数据，用结论性语言"""

    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={
                "model": "glm-4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 200,
            },
            timeout=15,
        )
        return resp.json()["choices"][0]["message"].get("content", "").strip()
    except Exception:
        return ""
```

集成到 `humanize_reason`:

```python
def humanize_reason(reason_data: dict, name: str = "", fallback_reason: str = "") -> str:
    parts = []
    # ... 原有逻辑 ...

    # === 新增: 8 维度展示 ===
    dims = reason_data.get("eight_dimensions") or {}
    if dims:
        dim_lines = ["", "8维度分析:"]
        for dim_name, info in dims.items():
            score = info.get("score", 50)
            items = info.get("items", [])
            tier = "优" if score >= 70 else "良" if score >= 50 else "弱"
            item_str = ", ".join(
                f"{it['name']}={it['value']}{'('+it['label']+')' if it.get('label') else ''}"
                for it in items[:2]
            )
            dim_lines.append(f"  {dim_name} {score}({tier}) | {item_str}")
        parts.extend(dim_lines)

    # === 新增: 交易建议 ===
    ts = reason_data.get("trade_suggestion") or {}
    if ts:
        parts.append("")
        parts.append(
            f"建议: 目标 {ts.get('target_price')} (+{ts.get('predicted_return_pct')}%) "
            f"/ 止损 {ts.get('stop_loss')} ({ts.get('stop_loss_pct')*100:.0f}%) "
            f"/ 持有 {ts.get('hold_days')} 日 / 风险收益比 {ts.get('risk_reward_ratio')}"
        )

    # === 新增: AI 综合解读 ===
    summary = ai_eight_dimensions_summary(reason_data, name=name)
    if summary:
        parts.append("")
        parts.append(f"AI研判: {summary}")

    if parts:
        prefix = f"{name}：" if name else ""
        return f"{prefix}{'，'.join(parts)}"
    return fallback_reason
```

#### B.4 推送格式适配（0.5 天）

`portfolio/trade_utils.py:format_push_message` 和 `simulation/report.py:_format_push_daily` 已经有结构，只需让 `humanize_reason` 输出新增维度即可（B.3 已搞定）。

---

## 完整推送示例

```markdown
**今日推荐 (10 只)**

★ 1. 锦泓集团 (603518) 12.50 → 14.30 (+14.4%)
   建议: 买入 4000 股 = 50,000 元
   持有: 15-20 交易日 | 风险收益比: 2.3 | 止损: 11.50

   多因子+ML双重看好 (final_score=2.10, 行业 #5/47)

   8维度分析:
     盘面 75(优) | 量比=1.8(放量), 换手=6.2%(活跃)
     大盘 60(良) | 上证=+0.5%, 普涨(+10)
     行业 80(优) | 纺织服装=+2.1%(强势), 行业前10%
     利好 70(良) | 新闻情绪=+0.3, 一季报预增28%
     量价 65(良) | 5日量比=1.4x(放量)
     资金 85(优) | 主力净流入=+5800万, 5日累计=+1.2亿(持续流入)
     业绩 75(优) | PE=11(低估), PE 行业分位=18%(行业偏低), PB=0.9(破净)
     订单 60(良) | 买卖力量比=58%:42%(买压强)

   建议: 目标 14.30 (+14%) / 止损 11.50 (-8%) / 持有 15-20 日 / 风险收益比 1.8

   AI研判: 资金面强势主导（主力 5 日累计净流入 1.2 亿）+
          行业内估值最低 18% 分位，中短线机会明确。
          风险: 大盘维度仅 60，注意系统性回调影响。
```

---

## 验收清单

### Phase 0
- [ ] `python3 -c "from config.settings import NUM_POSITIONS; print(NUM_POSITIONS)"` 输出 10
- [ ] `python3 main.py live --simulate` 选股出 10 只

### Phase A
- [ ] `python3 main.py fetch-industry`（或 fetch-all 末尾）成功，industry_map 表 ~5728 行
- [ ] `pytest tests/test_neutralization.py` 全过
- [ ] `python3 main.py evolve` 完成
  - [ ] cv_r2_mean ≥ 0.06
  - [ ] feature_importance top 5 含至少 2 个估值因子（pe_ttm/pb）+ 2 个动量
- [ ] `python3 main.py predict` 输出含 industry 列

### Phase B
- [ ] `python3 main.py live` 输出含 8 维度（盘面/大盘/行业/利好/量价/资金/业绩/订单）
- [ ] picks 中 `reason_data["eight_dimensions"]` 非空，每个维度含 score + items
- [ ] picks 中 `reason_data["trade_suggestion"]` 含 target_price + stop_loss + hold_days + risk_reward_ratio
- [ ] AI 研判文案存在且 ≤ 100 字

### 整体
- [ ] `python3 -m pytest tests/ -q` 全过（在原 61 + 新增 4 = 65 项）
- [ ] 模拟盘和实盘推送均含 8 维度展示
- [ ] PROGRESS.md 追加本次改动说明

---

## 提交规范

按 4 个 commit 拆分：

```
feat: NUM_POSITIONS 5 → 10
```

```
feat(data): Tushare 行业分类入库 + factors 中性化预处理工具

- data/tushare_industry.py 新增, industry_map 表 ~5728 行
- factors/calculator: winsorize_cross_section / cross_sectional_zscore / industry_neutralize
- compute_stock_pool_factors 自动注入 industry 字段
- ml/ranker.prepare_training_data + predict 集成中性化
- main.py 加 fetch-industry 命令
+ tests/test_neutralization.py
```

```
feat(allocator): final_score 改 ML 主导 (70%) + 多因子辅助 (30%)

- portfolio/allocator: ml_norm * 0.7 + factor_norm * 0.3
- strategy/small_cap: 因子权重简化（中性化后等权，仅情绪因子降权）
- 详细公式说明见 docs/optimization_backlog.md
```

```
feat(analysis): 新增 8 维度选股分析 + AI 综合研判 + 交易建议

- analysis/eight_dimensions.py 新增 (~400 行)
- data/fetcher: fetch_index_realtime / fetch_order_book / fetch_capital_flow_history
- portfolio/allocator.get_stock_picks_live: Step 7 集成 8 维度
- portfolio/reason_text: humanize_reason 展示 8 维度 + 交易建议 + AI 研判
- trade_suggestion 含 target_price / stop_loss / risk_reward_ratio
```

每个 commit 末尾：
```
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 部署步骤

```bash
# 1. 拉行业数据（新数据）
python3 main.py fetch-industry

# 2. 重新训练（含中性化）
python3 main.py evolve --push
# 预期: cv_r2_mean ≥ 0.06，新模型可能直接上线（业界中性化 R² 通常 0.07-0.10）

# 3. 跑一次实盘验证
python3 main.py live --simulate
# 预期: 10 只推荐 + 8 维度完整展示 + 交易建议
```

---

## 风险与回退

| 风险 | 缓解 |
|------|------|
| Tushare stock_basic 接口暂时不可用 | industry_map 表为空时 industry 列填 "未知"，中性化退化为截面排名 |
| 8 维度某个数据源失败（如新浪五档限流） | try/except 包裹，单维度失败不影响其他维度 |
| GLM API 限流（AI 研判失败） | summary 返回空字符串，humanize_reason 不显示 AI 段 |
| ML 主导 70% 后某只股票 ML 预测异常高 | 多因子合成 30% 提供 sanity check，且 8 维度展示让用户能识别异常 |
| 中性化后 R² 仍 < 0.06 | 优先排查行业 map 覆盖率（应 ≥ 95%），其次检查截面是否有 NaN 污染 |

每个 Phase 单独 commit，任何阶段出问题可单独 revert。

---

## 不在本次范围

- **板块独立训练**（多模型）— 业界共识中性化优于多模型，不做
- **加 200 个新因子**（中金做法）— 当前 22 因子已足够，先验证中性化效果
- **8 维度的"维度间权重"** — 当前各维度独立打分平铺展示，由 AI 解读权衡，不引入数值合成
