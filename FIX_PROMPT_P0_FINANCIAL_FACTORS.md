# 实施 Prompt — P0 财务因子（4 个，4 天工作量）

> 分支：`feature/simulated-trading`（基于 commit `e28371e`+ 后续）
> 目标：加 4 个高 ROI 财务因子，预期 cv_r2_mean 从当前 0.065 → **0.085-0.105**
> 工程量：~4 天，1 个完整 commit

---

## 项目背景

**当前状态**：
- v8 跑通: 5-50 亿小盘 + 22 因子 + 跳过情绪 + 无中性化 → R²=0.065
- 生产模型仍是 4 月初的 R²=0.0902
- 22 因子里 **0 个财务因子**（只有 pe_ttm/pb 估值因子）
- Tushare 5200 积分实测可用 `fina_indicator` 接口（探测验证完成）

**fina_indicator 实际字段**（已实测）：
```
roe              5.4242    净资产收益率（季度）
roe_yearly       10.8484   净资产收益率（年化）
debt_to_assets   91.6255   资产负债率（%）
or_yoy           4.6516    营业收入同比增速（%）← 注意是 or_yoy 不是 yoy_or
dt_eps_yoy       8.0645    扣非每股收益同比增速（%）
netprofit_yoy    3.0292    净利润同比增速（%）
op_yoy           2.8326    营业利润同比增速（%）
netprofit_margin 33.5516   净利率
assets_turn      0.0136    总资产周转率
ann_date         20240816  公告日 ← PIT 关键
end_date         20240630  财报截止日
```

---

## 4 个 P0 因子设计

| 因子 | Tushare 字段 | 业界 IC | 方向 | 备注 |
|------|-------------|---------|------|------|
| **roe** | `roe_yearly`（年化）| 0.04 | +1 | 优先用年化版（季度数据噪声大）|
| **revenue_growth** | `or_yoy` | 0.05 | +1 | 营收增速，最稳定的成长信号 |
| **eps_growth** | `dt_eps_yoy` | 0.04 | +1 | 扣非 EPS 增速（剔除一次性损益）|
| **debt_to_assets** | `debt_to_assets` | -0.03 | -1 | 负债率高 = 风险大，方向负 |

---

## Phase A：数据接入（1 天）

### A.1 新建 `data/financial_indicator.py`

```python
"""
Tushare 财务指标 (fina_indicator) — PIT 数据，按公告日 (ann_date) 入库

字段:
  ann_date         公告日 ← 训练时必须用此日期，避免未来数据泄露
  end_date         财报截止日 (用于报告期识别)
  roe_yearly       年化 ROE (%)
  or_yoy           营收同比增速 (%)
  dt_eps_yoy       扣非 EPS 同比增速 (%)
  debt_to_assets   资产负债率 (%)
  netprofit_yoy    备用：净利润同比增速

更新策略:
  - 历史回填：按股票批量拉 5 年数据
  - 每月增量：每月 1 号 + 季报披露窗口（4/8/10 月）拉新数据
"""

import os
import time
import sqlite3
import logging
import pandas as pd
from typing import Optional
from datetime import datetime

logger = logging.getLogger(__name__)
DB_PATH = "data/quant.db"

# fina_indicator 接口要的字段（多取以备扩展）
FINA_FIELDS = [
    "ts_code", "ann_date", "end_date",
    "roe_yearly", "or_yoy", "dt_eps_yoy", "debt_to_assets",
    "netprofit_yoy", "op_yoy", "netprofit_margin",
]


def _init_tushare():
    import tushare as ts
    from data.tushare_fundamentals import TUSHARE_TOKEN
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def _init_table(conn):
    """财务指标表（PIT 数据，按 code+ann_date 主键）"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS financial_indicator (
            code TEXT,
            ann_date TEXT,
            end_date TEXT,
            roe_yearly REAL,
            or_yoy REAL,
            dt_eps_yoy REAL,
            debt_to_assets REAL,
            netprofit_yoy REAL,
            op_yoy REAL,
            netprofit_margin REAL,
            updated_at TEXT,
            PRIMARY KEY (code, ann_date, end_date)
        )
    """)
    # 关键索引：按 (code, ann_date) 查询时性能
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fin_code_ann ON financial_indicator(code, ann_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fin_ann ON financial_indicator(ann_date)")
    conn.commit()


def fetch_one_stock(pro, ts_code: str, start_date: str = "20200101") -> pd.DataFrame:
    """拉单股财务指标历史"""
    try:
        df = pro.fina_indicator(
            ts_code=ts_code,
            start_date=start_date,
            fields=",".join(FINA_FIELDS),
        )
        if df is None or df.empty:
            return pd.DataFrame()
        return df
    except Exception as e:
        logger.warning(f"fina_indicator {ts_code}: {e}")
        return pd.DataFrame()


def save_batch(rows: list) -> int:
    """批量入库"""
    if not rows:
        return 0
    conn = sqlite3.connect(DB_PATH)
    try:
        _init_table(conn)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = [
            (r["code"], r["ann_date"], r["end_date"],
             r.get("roe_yearly"), r.get("or_yoy"),
             r.get("dt_eps_yoy"), r.get("debt_to_assets"),
             r.get("netprofit_yoy"), r.get("op_yoy"),
             r.get("netprofit_margin"),
             now)
            for r in rows
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO financial_indicator
            (code, ann_date, end_date, roe_yearly, or_yoy, dt_eps_yoy,
             debt_to_assets, netprofit_yoy, op_yoy, netprofit_margin, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            data,
        )
        conn.commit()
        return len(data)
    finally:
        conn.close()


def batch_fetch_all(start_date: str = "20200101", limit: int = 0):
    """
    批量回填所有股票的财务指标

    Parameters
    ----------
    start_date : str  起始日期 (YYYYMMDD)
    limit : int  限制股票数（调试用）
    """
    from data.storage import list_cached_stocks

    pro = _init_tushare()
    stocks = list_cached_stocks()
    if limit > 0:
        stocks = stocks[:limit]

    print(f"开始拉取 {len(stocks)} 只股票财务指标 (起始 {start_date})")
    print("Tushare fina_indicator 限流 200 次/分钟，预计 ~30 分钟")

    success = 0
    fail = 0
    total_rows = 0
    t0 = time.time()

    for i, code in enumerate(stocks, 1):
        # 转 ts_code 格式
        prefix = "SH" if code.startswith(("6", "5")) else "SZ"
        # 北交所 920/4xx/8xx
        if code.startswith(("4", "8", "92")):
            prefix = "BJ"
        ts_code = f"{code}.{prefix}"

        df = fetch_one_stock(pro, ts_code, start_date)
        if df.empty:
            fail += 1
        else:
            df = df[df["ann_date"].notna()]  # 去掉 ann_date 为空的脏数据
            df["code"] = code
            rows = df.to_dict("records")
            n = save_batch(rows)
            total_rows += n
            success += 1

        if i % 100 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed
            eta = (len(stocks) - i) / rate / 60
            print(f"  [{i}/{len(stocks)}] ok={success} fail={fail} 行数={total_rows} eta~{eta:.0f}min")

        # 限流: 200 次/分钟 → 0.3s/次
        time.sleep(0.3)

    print(f"\n完成: ok={success} fail={fail} 行数={total_rows}")


def get_latest_pit(code: str, as_of_date: str) -> dict:
    """
    获取股票在 as_of_date 时的最新可用财务数据 (PIT)

    Parameters
    ----------
    code : 股票代码
    as_of_date : 截面日期 (YYYY-MM-DD 或 YYYYMMDD)

    Returns
    -------
    dict: {roe_yearly, or_yoy, dt_eps_yoy, debt_to_assets, ...}
    无可用数据返回 {}
    """
    # 标准化日期格式
    as_of_date = as_of_date.replace("-", "")

    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            """SELECT roe_yearly, or_yoy, dt_eps_yoy, debt_to_assets,
                      netprofit_yoy, op_yoy, netprofit_margin
            FROM financial_indicator
            WHERE code = ? AND ann_date <= ?
            ORDER BY ann_date DESC, end_date DESC
            LIMIT 1""",
            (code, as_of_date),
        )
        row = cur.fetchone()
        if not row:
            return {}
        return {
            "roe_yearly": row[0],
            "or_yoy": row[1],
            "dt_eps_yoy": row[2],
            "debt_to_assets": row[3],
            "netprofit_yoy": row[4],
            "op_yoy": row[5],
            "netprofit_margin": row[6],
        }
    finally:
        conn.close()


def load_all_pit_to_dict() -> dict:
    """
    一次性加载所有 PIT 数据到内存 → {(code, ann_date): factor_dict}

    用于训练时高效查询，避免每条样本一次 SQL
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            """SELECT code, ann_date, roe_yearly, or_yoy, dt_eps_yoy,
                      debt_to_assets
               FROM financial_indicator
               ORDER BY code, ann_date"""
        )
        result = {}
        for row in cur.fetchall():
            code, ann_date = row[0], row[1]
            result[(code, ann_date)] = {
                "roe_yearly": row[2],
                "or_yoy": row[3],
                "dt_eps_yoy": row[4],
                "debt_to_assets": row[5],
            }
        return result
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def get_coverage() -> dict:
    """统计覆盖率"""
    conn = sqlite3.connect(DB_PATH)
    try:
        result = conn.execute("""
            SELECT
                COUNT(*) as total_rows,
                COUNT(DISTINCT code) as unique_codes,
                MIN(ann_date) as min_ann,
                MAX(ann_date) as max_ann,
                COUNT(roe_yearly) as roe_non_null,
                COUNT(or_yoy) as or_yoy_non_null
            FROM financial_indicator
        """).fetchone()
        return dict(zip(
            ["total_rows", "unique_codes", "min_ann", "max_ann",
             "roe_non_null", "or_yoy_non_null"],
            result,
        ))
    finally:
        conn.close()


def run():
    """命令行入口"""
    batch_fetch_all()


if __name__ == "__main__":
    run()
```

### A.2 `main.py` 加 fetch-financial 命令

```python
elif command == "fetch-financial":
    from data.financial_indicator import batch_fetch_all
    _limit = 0
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        _limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 100
    batch_fetch_all(limit=_limit)
```

### A.3 验收

```bash
# 小量测试（10 只股票）
python3 main.py fetch-financial --limit 10

# 检查数据
python3 -c "
from data.financial_indicator import get_coverage
print(get_coverage())
"
# 预期: total_rows ≥ 100, unique_codes = 10, min_ann ~20200131, max_ann ~20260430

# 全量回填
python3 main.py fetch-financial
# 预计 30-40 分钟
```

---

## Phase B：因子计算 + 训练集成（1 天）

### B.1 修改 `factors/calculator.py:compute_all_factors`

在 `compute_all_factors()` 函数末尾追加（基本面因子已读完后）：

```python
def compute_all_factors(symbol: str, end_date: str = None, lookback: int = 120) -> dict:
    ...
    # 现有逻辑：读 K 线 + 因子计算
    factors = {"code": symbol}
    factors.update(calc_momentum(df))
    ...

    # 基本面因子: 直接从 SQLite 读
    last_row = df.iloc[-1]
    for col in ["pe_ttm", "pb", "turnover_rate", "volume_ratio"]:
        factors[col] = last_row.get(col, np.nan)

    # === 新增: 财务因子 (PIT 查询) ===
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")
    try:
        from data.financial_indicator import get_latest_pit
        fin = get_latest_pit(symbol, end_date)
        factors["roe_yearly"] = fin.get("roe_yearly", np.nan)
        factors["or_yoy"] = fin.get("or_yoy", np.nan)
        factors["dt_eps_yoy"] = fin.get("dt_eps_yoy", np.nan)
        factors["debt_to_assets"] = fin.get("debt_to_assets", np.nan)
    except Exception:
        for col in ["roe_yearly", "or_yoy", "dt_eps_yoy", "debt_to_assets"]:
            factors[col] = np.nan

    return factors
```

### B.2 修改 `ml/ranker.py:prepare_training_data`

在每条训练样本生成时，按 `end_date` 查 PIT 数据：

```python
def prepare_training_data(...):
    ...
    # 全局缓存（与 _SENT_CACHE 类似）
    global _FIN_CACHE
    if "_FIN_CACHE" not in globals():
        from data.financial_indicator import load_all_pit_to_dict
        logger.info("加载 financial_indicator 到内存缓存...")
        _FIN_CACHE = load_all_pit_to_dict()
        logger.info(f"  财务缓存: {len(_FIN_CACHE)} 条 PIT 记录")

    for i, sym in enumerate(symbols):
        ...
        for end_idx in range(60, len(df) - forward_days, 20):
            ...
            end_date = str(window.iloc[-1]["date"])[:10]

            factors = {"code": sym, "label": forward_return, "end_date": end_date}
            factors.update(calc_momentum(window))
            ...

            # === 新增: PIT 查询财务因子 ===
            fin_factors = _lookup_financial_pit(sym, end_date.replace("-", ""))
            factors["roe_yearly"] = fin_factors.get("roe_yearly", np.nan)
            factors["or_yoy"] = fin_factors.get("or_yoy", np.nan)
            factors["dt_eps_yoy"] = fin_factors.get("dt_eps_yoy", np.nan)
            factors["debt_to_assets"] = fin_factors.get("debt_to_assets", np.nan)

            # 情绪因子 (现状)
            factors["sentiment_score"] = _lookup_historical_sentiment(sym, end_date)
            ...


def _lookup_financial_pit(code: str, as_of_yyyymmdd: str) -> dict:
    """
    PIT 查询: 找该股票在 as_of_date 之前最近一次公告

    用全局缓存优化性能 (35 万样本 × 字典查询 ~1 秒)
    """
    global _FIN_CACHE
    if "_FIN_CACHE" not in globals():
        return {}

    # 缓存按 (code, ann_date) 索引，需找该 code 下 ann_date <= as_of_date 的最大值
    # 简化：在 _FIN_CACHE 中线性查找（35 万样本，加上股票预筛后实际 60+ 条/股，OK）
    candidates = [
        (ann_date, factors)
        for (c, ann_date), factors in _FIN_CACHE.items()
        if c == code and ann_date <= as_of_yyyymmdd
    ]
    if not candidates:
        return {}
    # 取最近一次公告
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]
```

⚠️ **性能优化**：上述线性查找在 35 万样本下太慢。改用预排序索引：

```python
# 改进版：缓存改为 {code: [(ann_date, factors), ...]}（每只股票按 ann_date 排序）
def load_all_pit_to_dict() -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            """SELECT code, ann_date, roe_yearly, or_yoy, dt_eps_yoy,
                      debt_to_assets
               FROM financial_indicator
               ORDER BY code, ann_date"""
        )
        result = {}  # code → [(ann_date, dict), ...]
        for row in cur.fetchall():
            code = row[0]
            entry = (row[1], {
                "roe_yearly": row[2], "or_yoy": row[3],
                "dt_eps_yoy": row[4], "debt_to_assets": row[5],
            })
            result.setdefault(code, []).append(entry)
        return result
    finally:
        conn.close()


def _lookup_financial_pit(code: str, as_of_yyyymmdd: str) -> dict:
    """O(log n) PIT 查询（每股 ~60 条历史，二分查找）"""
    global _FIN_CACHE
    if "_FIN_CACHE" not in globals():
        return {}
    history = _FIN_CACHE.get(code, [])
    if not history:
        return {}
    # 二分查找最近一次 ann_date <= as_of
    import bisect
    ann_dates = [h[0] for h in history]
    idx = bisect.bisect_right(ann_dates, as_of_yyyymmdd) - 1
    if idx < 0:
        return {}
    return history[idx][1]
```

### B.3 修改 `ml/ranker.py:FEATURE_COLS`

```python
FEATURE_COLS = [
    "mom_5d", "mom_10d", "mom_20d", "mom_60d",
    "vol_10d", "vol_20d",
    "avg_turnover_5d", "avg_turnover_20d", "turnover_accel",
    "vol_price_diverge", "volume_surge",
    "ma5_bias", "ma10_bias", "ma20_bias", "rsi_14",
    "pe_ttm", "pb", "turnover_rate", "volume_ratio",
    "sentiment_score",
    # P0 财务因子 (新增)
    "roe_yearly",
    "or_yoy",
    "dt_eps_yoy",
    "debt_to_assets",
]
```

### B.4 修改 `strategy/small_cap.py:_score_stocks` 因子方向

```python
factor_direction = {
    # ... 现有 ...
    # P0 财务因子方向
    "roe_yearly": 1,        # ROE 越高越好
    "or_yoy": 1,            # 营收增速越高越好
    "dt_eps_yoy": 1,        # EPS 增速越高越好
    "debt_to_assets": -1,   # 负债率越低越好
}

# 在 weight 部分加：
if factor_name in ("roe_yearly", "or_yoy", "dt_eps_yoy"):
    weight = 1.5  # 财务核心因子，给中等权重
elif factor_name == "debt_to_assets":
    weight = 1.0
```

### B.5 修改 `portfolio/allocator.py` reason_data 携带新因子

`get_stock_picks_live` 中 `reason_data["key_factors"]` 加 4 项（让推送展示）：

```python
"key_factors": {
    "mom_20d": row.get("mom_20d"),
    "pe_ttm": row.get("pe_ttm"),
    "pb": row.get("pb"),
    "vol_10d": row.get("vol_10d"),
    "turnover_rate": row.get("turnover_rate"),
    # 新增财务因子展示
    "roe_yearly": row.get("roe_yearly"),
    "or_yoy": row.get("or_yoy"),
    "debt_to_assets": row.get("debt_to_assets"),
},
```

### B.6 `portfolio/reason_text.py` 增加财务因子翻译

```python
# 在 humanize_reason 主体函数中追加：
roe = kf.get("roe_yearly")
if roe is not None:
    try:
        v = float(roe)
        if v > 15:
            parts.append(f"高 ROE({v:.0f}%)")
        elif v > 8:
            parts.append(f"ROE 良好({v:.0f}%)")
        elif v < 0:
            parts.append(f"亏损 ROE({v:.0f}%)")
    except (ValueError, TypeError):
        pass

or_yoy = kf.get("or_yoy")
if or_yoy is not None:
    try:
        v = float(or_yoy)
        if v > 30:
            parts.append(f"营收高增({v:.0f}%)")
        elif v > 10:
            parts.append(f"营收增长({v:.0f}%)")
        elif v < -10:
            parts.append(f"营收下滑({v:.0f}%)")
    except (ValueError, TypeError):
        pass

debt = kf.get("debt_to_assets")
if debt is not None:
    try:
        v = float(debt)
        if v > 80:
            parts.append(f"高负债({v:.0f}%)")
        elif v < 30:
            parts.append(f"低负债({v:.0f}%)")
    except (ValueError, TypeError):
        pass
```

---

## Phase C：每日/每月增量更新（0.5 天）

财务数据低频，每月跑一次即可：

### C.1 新建 `scripts/financial_monthly.py`

```python
"""
财务指标每月增量更新

每月 1 号 + 4/8/10 月（季报/中报/三季报披露窗口）跑一次

入口: bash scripts/financial_monthly.sh
"""
from datetime import datetime
from data.financial_indicator import batch_fetch_all


def run_monthly():
    # 仅拉最近 90 天的新数据
    end = datetime.now()
    start = (end.replace(day=1) - pd.DateOffset(months=3)).strftime("%Y%m%d")
    print(f"财务增量更新: {start} ~ 至今")
    batch_fetch_all(start_date=start)


if __name__ == "__main__":
    run_monthly()
```

### C.2 crontab 配置

```bash
# /etc/crontab 加一行
# 每月 1 号 17:00 跑（避开开盘 + evolve 16:00）
0 17 1 * * cd /home/ubuntu/pj_quant && python3 scripts/financial_monthly.py >> logs/financial.log 2>&1
```

---

## Phase D：单元测试 + 验收（0.5 天）

### D.1 新建 `tests/test_financial_indicator.py`

```python
"""测试财务因子接入"""
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd


def test_save_and_load_pit():
    """测试 save_batch + get_latest_pit"""
    from data.financial_indicator import save_batch, get_latest_pit

    save_batch([{
        "code": "TEST_001", "ann_date": "20240430", "end_date": "20240331",
        "roe_yearly": 12.5, "or_yoy": 8.3,
        "dt_eps_yoy": 5.0, "debt_to_assets": 45.0,
    }])

    # PIT 查询：截面 2024-05-01 应能查到 4-30 公告的数据
    result = get_latest_pit("TEST_001", "20240501")
    assert result["roe_yearly"] == 12.5
    assert result["or_yoy"] == 8.3

    # 截面 2024-04-29 应查不到（公告日是 4-30，未来数据）
    result = get_latest_pit("TEST_001", "20240429")
    assert result == {}


def test_pit_takes_latest():
    """同一股票多份公告，取最近的"""
    from data.financial_indicator import save_batch, get_latest_pit

    save_batch([
        {"code": "TEST_002", "ann_date": "20240430", "end_date": "20240331",
         "roe_yearly": 10.0},
        {"code": "TEST_002", "ann_date": "20240825", "end_date": "20240630",
         "roe_yearly": 12.0},
    ])

    # 截面 2024-09-01 应取 8-25 那一份
    result = get_latest_pit("TEST_002", "20240901")
    assert result["roe_yearly"] == 12.0


def test_lookup_with_cache():
    """测试 _FIN_CACHE 全局缓存的 PIT 查询"""
    from ml.ranker import _lookup_financial_pit
    import ml.ranker as ranker_mod

    # 注入测试缓存
    ranker_mod._FIN_CACHE = {
        "000001": [
            ("20240430", {"roe_yearly": 5.0, "or_yoy": 3.0,
                         "dt_eps_yoy": 2.0, "debt_to_assets": 90.0}),
            ("20240825", {"roe_yearly": 6.0, "or_yoy": 4.0,
                         "dt_eps_yoy": 3.0, "debt_to_assets": 89.0}),
        ]
    }
    result = _lookup_financial_pit("000001", "20240901")
    assert result["roe_yearly"] == 6.0
    result_early = _lookup_financial_pit("000001", "20240501")
    assert result_early["roe_yearly"] == 5.0
    result_too_early = _lookup_financial_pit("000001", "20240101")
    assert result_too_early == {}
```

### D.2 完整验收 evolve

```bash
# 1. 全量数据回填
python3 main.py fetch-financial
# 预期: 5500 只 × ~24 季度 = ~130000 行，~30 分钟

# 2. 验证覆盖
python3 -c "
from data.financial_indicator import get_coverage
print(get_coverage())
"
# 预期: total_rows > 100000, unique_codes > 4000

# 3. 单元测试
pytest tests/test_financial_indicator.py -v

# 4. 跑 evolve
python3 main.py evolve
# 预期:
#   - cv_r2_mean ≥ 0.085 (vs 当前 0.065，提升 +0.02)
#   - feature_importance top 5 应含至少 1 个财务因子（roe_yearly / or_yoy）
```

---

## Commit 规范

单 commit:

```
feat(factor): 加 P0 财务因子（ROE / 营收增速 / EPS 增速 / 负债率）

数据底座:
- data/financial_indicator.py: Tushare fina_indicator 接入 + SQLite PIT 表
- main.py: fetch-financial 命令
- crontab: 每月增量 (scripts/financial_monthly.py)

因子集成:
- factors/calculator.compute_all_factors: PIT 查询 4 个财务因子
- ml/ranker.prepare_training_data: 全局缓存 + 二分查找加速 PIT 查询
- ml/ranker.FEATURE_COLS: 加 roe_yearly/or_yoy/dt_eps_yoy/debt_to_assets
- strategy/small_cap.factor_direction: 4 因子方向 + 1.5x 权重
- portfolio/allocator.reason_data.key_factors: 携带 4 因子展示
- portfolio/reason_text: 推送翻译（高 ROE / 营收高增 / 高负债等）

测试: tests/test_financial_indicator.py 3 项

预期效果:
- cv_r2_mean: 0.065 → 0.085-0.105
- 推送理由含财务质量信号

PIT 数据正确性: 训练时按 ann_date 过滤，避免未来数据泄露

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 风险与回退

| 风险 | 缓解 |
|------|------|
| Tushare 限流 200 次/分钟 | 0.3s/次 sleep，预计 30 分钟完成 |
| 财务数据 ann_date 缺失（脏数据）| 入库前 `df = df[df["ann_date"].notna()]` |
| 财报修正后 (ann_date 相同 end_date 不同) | PRIMARY KEY 含 end_date，新版本会 INSERT 不冲突 |
| 北交所/科创板新股财务数据少 | 预期 NaN，XGBoost 自动用 median 填充 |
| 因子加入后 R² 不升反降 | feature_importance 排查 + 单因子 IC 验证 + 必要时回滚 |
| _FIN_CACHE 内存占用大（5500 股 × 60 季度 ≈ 33 万 dict）| 每只股票 ~6KB → 总 200MB，可接受 |

回退：单 commit revert 即可。

---

## 不在本次范围

- 加更多财务因子（毛利率、流动比率、ROIC 等 P3）→ 等 P0 R² 验证后再扩
- 北向资金 / 融资融券 P1 因子 → 单独 prompt
- 龙虎榜 / 研报 P2 因子 → 单独 prompt
- 中性化重新启用（仍 default off，无确凿收益证据）

---

## 部署步骤（云主机）

```bash
ssh 云主机
cd /home/ubuntu/pj_quant
git pull origin feature/simulated-trading

# 1. 拉财务数据（云主机和本机分开拉，互不影响）
python3 main.py fetch-financial

# 2. evolve 验证
python3 main.py evolve --push
# 推送报告应显示 R² 提升，新模型 candidate

# 3. cron 配置（每月增量）
crontab -e
# 加: 0 17 1 * * cd /home/ubuntu/pj_quant && python3 scripts/financial_monthly.py >> logs/financial.log 2>&1
```
