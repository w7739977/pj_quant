# 实施 Prompt — 情绪因子 B+C 全方案（FinBERT 替代 + sentiment_history 库）

> 分支：`feature/simulated-trading`
> 目标：让情绪因子真正对 ML 决策有贡献（当前 importance=0%）
> 总工程量：~10 天，分两阶段（B: 1-2 天，C: 6-7 天）

---

## 项目背景

**痛点**：
1. 情绪因子在 ML 模型中 `feature_importance=0.0000`（贡献 0%）
2. GLM-4-flash 高频抓取 5491 只股票时被限流
3. `prepare_training_data` 拿不到历史新闻数据，训练时 `sentiment_score` 全 NaN
4. 业界共识做法：**FinBERT 离线批处理 + sentiment_history 持久化**

**现状架构**（评估后）：
```
sentiment/analyzer.py:
  fetch_stock_news(symbol)        — 东方财富搜索 API，单股一次 HTTP
  flash_tag_sentiment(texts)      — GLM-4-flash 批量打分（限流痛点）
  glm5_deep_analysis(news)        — GLM-5 推理（市场情绪用）
  rule_based_sentiment(text)      — 关键词降级（已有）

factors/calculator.py:
  _batch_sentiment_factors()      — 训练用 batch 调 GLM
  calc_sentiment_factor()         — 单股调 GLM
```

**改造目标**：
1. 加 FinBERT-Chinese 离线推理（**Stage B**）
2. 建 `sentiment_history` 表 + 历史新闻 + 历史回填（**Stage C**）
3. 模型层：训练时 JOIN 表，推理时实时打分

---

## Stage B：FinBERT-Chinese 替代 GLM（1-2 天）

### B.1 依赖 + 模型下载（0.5 天）

**新增依赖**: `requirements.txt` 追加：
```
transformers>=4.35.0
torch>=2.0.0
huggingface-hub>=0.20.0
```

注意：`torch` CPU 版 ~200MB，加上 transformers 约 500MB 总量。

**模型选择**（按推荐度）：

| 模型 | size | 准确率 | 备注 |
|------|------|--------|------|
| `yiyanghkust/finbert-tone-chinese` | 400MB | 73% | **推荐**，A 股研报 8k 训练 |
| `bardsai/finance-sentiment-zh` | 400MB | 70% | 通用财经 |
| `IDEA-CCNL/Erlangshen-RoBERTa-110M-Sentiment` | 400MB | 75% | 通用情感（非金融） |

**推荐 finbert-tone-chinese**（金融语料 + A 股精准微调）。

### B.2 新建本地推理模块（0.5 天）

**新建文件**: `sentiment/finbert_local.py`

```python
"""
FinBERT-Chinese 本地推理 — 零限流、零成本、CPU 批处理 ~100ms / 32 条

模型: yiyanghkust/finbert-tone-chinese
首次运行会下载 ~400MB 到 ~/.cache/huggingface/

用法:
  from sentiment.finbert_local import score_texts, score_one
  scores = score_texts(["茅台业绩超预期", "暴雷股退市"])
  # 输出: [0.92, -0.85]
"""
import os
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)

# 全局单例（模型加载一次常驻）
_PIPELINE = None
_MODEL_NAME = os.getenv("FINBERT_MODEL", "yiyanghkust/finbert-tone-chinese")


def _get_pipeline():
    """懒加载 + 单例"""
    global _PIPELINE
    if _PIPELINE is None:
        try:
            from transformers import pipeline
            logger.info(f"加载 FinBERT 模型: {_MODEL_NAME}")
            _PIPELINE = pipeline(
                "sentiment-analysis",
                model=_MODEL_NAME,
                device=-1,  # CPU; 有 GPU 改为 0
                truncation=True,
                max_length=128,  # 标题足够，长内容截断
            )
            logger.info("FinBERT 加载完成")
        except Exception as e:
            logger.error(f"FinBERT 加载失败: {e}")
            raise
    return _PIPELINE


def score_texts(texts: List[str], batch_size: int = 32) -> List[float]:
    """
    批量打分文本 → 情绪分数 [-1, 1]

    Parameters
    ----------
    texts : 文本列表（标题或短句）
    batch_size : 批次大小（CPU 32, GPU 64-128）

    Returns
    -------
    list of float, 长度等于 texts。失败的位置返回 0.0。
    """
    if not texts:
        return []
    try:
        clf = _get_pipeline()
    except Exception:
        return [0.0] * len(texts)

    scores = []
    try:
        # transformers pipeline 自动 batch
        results = clf(texts, batch_size=batch_size)
        for r in results:
            label = r.get("label", "").lower()
            score = float(r.get("score", 0.5))
            # 标签映射 (finbert-tone-chinese: positive/negative/neutral)
            if "positive" in label or "pos" in label:
                scores.append(score)  # 0.5~1.0
            elif "negative" in label or "neg" in label:
                scores.append(-score)
            else:
                scores.append(0.0)  # neutral
    except Exception as e:
        logger.warning(f"FinBERT 推理失败: {e}")
        return [0.0] * len(texts)
    return scores


def score_one(text: str) -> float:
    """单文本打分快捷方法"""
    if not text:
        return 0.0
    return score_texts([text])[0]


def is_available() -> bool:
    """检查 FinBERT 是否可用（首次会下载，可能慢）"""
    try:
        _get_pipeline()
        return True
    except Exception:
        return False
```

### B.3 改造现有调用方（0.5 天）

**修改 `sentiment/analyzer.py:flash_tag_sentiment`**：FinBERT 优先，GLM 兜底。

```python
def flash_tag_sentiment(texts: list[str]) -> list[float]:
    """
    批量给文本打情绪分

    优先级: FinBERT 本地（零限流）→ GLM-4-flash → 关键词规则
    """
    if not texts:
        return []

    # 优先 FinBERT 本地推理
    try:
        from sentiment.finbert_local import score_texts, is_available
        if is_available():
            scores = score_texts(texts)
            if scores and any(s != 0 for s in scores):
                logger.debug(f"FinBERT 打分 {len(texts)} 条")
                return scores
    except Exception as e:
        logger.warning(f"FinBERT 调用失败，降级 GLM: {e}")

    # 降级 1: GLM-4-flash
    news_block = "\n".join(f"{i+1}. {t[:100]}" for i, t in enumerate(texts))
    prompt = f"""给以下{len(texts)}条A股相关新闻打情绪分..."""
    content = _call_llm("glm-4-flash", prompt, max_tokens=300, temperature=0.3)
    scores = _parse_scores(content, len(texts))
    if scores is not None:
        return scores

    # 降级 2: 关键词规则
    logger.info("LLM 全部失败，降级到规则模式")
    return [rule_based_sentiment(t) for t in texts]
```

**修改 `factors/calculator.py:_batch_sentiment_factors`**：去掉 GLM batch prompt 拼接，直接调 FinBERT。

```python
def _batch_sentiment_factors(factor_df: pd.DataFrame) -> pd.DataFrame:
    """
    批量计算情绪因子（FinBERT 优先）
    """
    from sentiment.analyzer import fetch_stock_news, flash_tag_sentiment

    df = factor_df.copy()
    df["sentiment_score"] = 0.0
    df["sentiment_count"] = 0

    symbols = df["code"].tolist()

    # 阶段 1: 抓新闻（仍是串行，下次优化）
    stock_titles = {}
    for sym in symbols:
        try:
            news = fetch_stock_news(sym)
            if news:
                stock_titles[sym] = [n["title"] for n in news[:3]]
        except Exception:
            pass

    if not stock_titles:
        return df

    # 阶段 2: 拼成大批量喂 FinBERT 一次推理
    all_titles = []
    sym_offsets = {}  # sym → (start, end) 位置
    cursor = 0
    for sym, titles in stock_titles.items():
        sym_offsets[sym] = (cursor, cursor + len(titles))
        all_titles.extend(titles)
        cursor += len(titles)

    # 一次批量打分（FinBERT 自动 batch_size=32）
    scores = flash_tag_sentiment(all_titles)

    # 按股票聚合
    for sym, (start, end) in sym_offsets.items():
        sym_scores = scores[start:end]
        if sym_scores:
            avg = float(np.mean(sym_scores))
            idx = df.index[df["code"] == sym]
            if len(idx) > 0:
                df.loc[idx[0], "sentiment_score"] = round(avg, 3)
                df.loc[idx[0], "sentiment_count"] = len(sym_scores)

    has_sentiment = (df["sentiment_count"] > 0).sum()
    logger.info(f"情绪因子完成: {has_sentiment}/{len(df)} 只 (FinBERT)")
    return df
```

**性能预期**：
- GLM 当前: 5491 只 × ~7 秒/批 = ~32 分钟
- FinBERT: 5491 只 × 0.1 秒（CPU 批处理）= **~5 分钟**

### B.4 单元测试

**新建 `tests/test_finbert.py`**:

```python
"""测试 FinBERT 本地推理"""
import pytest
from sentiment.finbert_local import score_texts, score_one


@pytest.fixture(scope="module")
def model_loaded():
    """首次加载模型（pytest 复用）"""
    from sentiment.finbert_local import is_available
    if not is_available():
        pytest.skip("FinBERT 不可用（首次运行需下载 ~400MB）")


def test_positive_news(model_loaded):
    """利好新闻应得正分"""
    score = score_one("贵州茅台业绩超预期，营收增长 30%")
    assert score > 0.3, f"利好应正分，实际 {score}"


def test_negative_news(model_loaded):
    """利空新闻应得负分"""
    score = score_one("某股暴雷退市，投资者血本无归")
    assert score < -0.3, f"利空应负分，实际 {score}"


def test_batch_consistency(model_loaded):
    """批量与单条结果应一致"""
    texts = ["业绩大涨创新高", "亏损扩大跌停"]
    batch_scores = score_texts(texts)
    single_scores = [score_one(t) for t in texts]
    for b, s in zip(batch_scores, single_scores):
        assert abs(b - s) < 0.01


def test_empty_input():
    assert score_texts([]) == []
    assert score_one("") == 0.0
```

### B.5 验收

```bash
# 1. 安装依赖（首次约 5 分钟）
pip install -r requirements.txt

# 2. 首次加载模型（自动下载约 400MB，5-10 分钟）
python3 -c "from sentiment.finbert_local import is_available; print(is_available())"
# 预期: True

# 3. 单元测试
pytest tests/test_finbert.py -v

# 4. 跑一次 evolve 测速度
python3 main.py evolve
# 预期: 情绪因子阶段从 ~32min 降到 ~5min
```

---

## Stage C：sentiment_history 数据库（6-7 天）

让训练阶段真正用到历史情绪。

### C.1 数据库设计（0.5 天）

**新建文件**: `data/sentiment_history.py`

```python
"""
情绪历史数据库 — 让 ML 训练能 JOIN 历史情绪

Schema:
  sentiment_history (date, code, score, news_count, source, updated_at)
  PRIMARY KEY (date, code)

数据流:
  公司公告 (Tushare anns_d) ──┐
  券商研报 (Tushare report)   ├─→ FinBERT 批处理 → sentiment_history
  财经新闻 (东方财富)         ┘
"""

import os
import sqlite3
import time
import logging
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)
DB_PATH = "data/quant.db"


def _init_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sentiment_history (
            date TEXT,
            code TEXT,
            score REAL,
            news_count INTEGER DEFAULT 0,
            sources TEXT DEFAULT '[]',
            updated_at TEXT,
            PRIMARY KEY (date, code)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sent_date ON sentiment_history(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sent_code ON sentiment_history(code)")
    conn.commit()


def save_sentiment(date: str, code: str, score: float,
                   news_count: int = 0, sources: list = None) -> None:
    """单条 UPSERT"""
    conn = sqlite3.connect(DB_PATH)
    try:
        _init_table(conn)
        import json
        sources_json = json.dumps(sources or [], ensure_ascii=False)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR REPLACE INTO sentiment_history "
            "(date, code, score, news_count, sources, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, code, score, news_count, sources_json, now),
        )
        conn.commit()
    finally:
        conn.close()


def save_sentiment_batch(rows: list) -> int:
    """
    批量 UPSERT
    rows: [{date, code, score, news_count, sources}, ...]
    Returns: 成功条数
    """
    if not rows:
        return 0
    conn = sqlite3.connect(DB_PATH)
    try:
        _init_table(conn)
        import json
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data = [
            (r["date"], r["code"], r.get("score", 0.0),
             r.get("news_count", 0),
             json.dumps(r.get("sources", []), ensure_ascii=False),
             now)
            for r in rows
        ]
        conn.executemany(
            "INSERT OR REPLACE INTO sentiment_history "
            "(date, code, score, news_count, sources, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            data,
        )
        conn.commit()
        return len(data)
    finally:
        conn.close()


def load_sentiment(code: str, start: str = None, end: str = None) -> pd.DataFrame:
    """读取某只股票历史情绪"""
    conn = sqlite3.connect(DB_PATH)
    try:
        sql = "SELECT date, code, score, news_count FROM sentiment_history WHERE code = ?"
        params = [code]
        if start:
            sql += " AND date >= ?"
            params.append(start)
        if end:
            sql += " AND date <= ?"
            params.append(end)
        sql += " ORDER BY date"
        return pd.read_sql(sql, conn, params=params)
    except Exception:
        return pd.DataFrame()
    finally:
        conn.close()


def load_sentiment_for_date(date: str) -> dict:
    """读取某一天全市场情绪 → {code: score}"""
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute(
            "SELECT code, score FROM sentiment_history WHERE date = ?",
            (date,),
        )
        return {code: score for code, score in cur.fetchall()}
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


def get_coverage() -> dict:
    """统计覆盖情况"""
    conn = sqlite3.connect(DB_PATH)
    try:
        result = conn.execute("""
            SELECT
                COUNT(*) as total_rows,
                COUNT(DISTINCT code) as unique_codes,
                COUNT(DISTINCT date) as unique_dates,
                MIN(date) as min_date,
                MAX(date) as max_date
            FROM sentiment_history
        """).fetchone()
        return {
            "total_rows": result[0],
            "unique_codes": result[1],
            "unique_dates": result[2],
            "min_date": result[3],
            "max_date": result[4],
        }
    finally:
        conn.close()
```

### C.2 历史新闻获取（2 天）

**Tushare 新闻接口**（已付费 token 可用）：
- `pro.news(start_date, end_date, src='sina')`：财经新闻流
- `pro.anns_d(ts_code, start_date, end_date)`：公司公告
- `pro.report_rc(ts_code)`：券商研报

**新建文件**: `data/historical_news.py`

```python
"""
历史新闻批量拉取 — Tushare 新闻流 + 公司公告

策略:
  1. 先拉财经新闻（覆盖广，但需匹配股票代码）
  2. 公司公告（精确到股票，质量最高）
  3. 券商研报（专业，时效低）
"""
import os
import time
import logging
import pandas as pd
from typing import Optional

logger = logging.getLogger(__name__)
NEWS_PARQUET_DIR = "data/historical_news_parquet"


def _init_tushare():
    import tushare as ts
    from data.tushare_fundamentals import TUSHARE_TOKEN
    ts.set_token(TUSHARE_TOKEN)
    return ts.pro_api()


def fetch_news_by_date(pro, trade_date: str, src: str = "sina") -> pd.DataFrame:
    """
    按日期拉取财经新闻流
    src: sina/wallstreetcn/10jqka/eastmoney/yuncaijing
    """
    try:
        df = pro.news(start_date=f"{trade_date} 00:00:00",
                      end_date=f"{trade_date} 23:59:59",
                      src=src)
        return df
    except Exception as e:
        logger.warning(f"news {trade_date} {src}: {e}")
        return pd.DataFrame()


def fetch_announcements_by_date(pro, trade_date: str) -> pd.DataFrame:
    """按日期拉公告（覆盖全市场）"""
    try:
        df = pro.anns_d(start_date=trade_date, end_date=trade_date)
        return df
    except Exception:
        return pd.DataFrame()


def batch_fetch_to_parquet(start: str, end: str):
    """
    历史新闻批量拉取到 Parquet（避免重复抓取）

    用法: batch_fetch_to_parquet("20200101", "20260430")
    时间预估: ~1500 个交易日 × 2-3 秒/天 = 1.5 小时
    """
    os.makedirs(NEWS_PARQUET_DIR, exist_ok=True)
    pro = _init_tushare()

    dates = pro.trade_cal(exchange="SSE", is_open="1",
                          start_date=start, end_date=end)["cal_date"].tolist()

    for date in dates:
        out_path = os.path.join(NEWS_PARQUET_DIR, f"{date}.parquet")
        if os.path.exists(out_path):
            continue

        # 公告 + 新浪新闻聚合
        anns = fetch_announcements_by_date(pro, date)
        news = fetch_news_by_date(pro, date, src="sina")

        # 合并标准化
        combined = []
        if not anns.empty:
            anns = anns.rename(columns={"ann_date": "date", "ts_code": "code", "title": "text"})
            anns["source"] = "公告"
            combined.append(anns[["date", "code", "text", "source"]])
        if not news.empty:
            # news 没有 code 列，需要后续从文本中匹配股票
            news = news.rename(columns={"datetime": "date", "title": "text"})
            news["code"] = ""
            news["source"] = "新闻"
            combined.append(news[["date", "code", "text", "source"]])

        if combined:
            df_all = pd.concat(combined, ignore_index=True)
            df_all.to_parquet(out_path, index=False)
            logger.info(f"  {date}: 公告{len(anns)} + 新闻{len(news)} → parquet")

        time.sleep(0.35)  # 避免限流
```

### C.3 历史情绪批量打分（2-3 天）

**新建文件**: `data/sentiment_backfill.py`

```python
"""
历史情绪批量回填 — Parquet 新闻 → FinBERT → SQLite sentiment_history

执行: python3 -c "from data.sentiment_backfill import run_backfill; run_backfill()"
预估: 1500 日 × 100 条/日 = 15 万条 × FinBERT batch ~0.1s/32 = ~10 分钟（CPU）
"""
import os
import logging
import pandas as pd
import re
from collections import defaultdict
from data.sentiment_history import save_sentiment_batch
from sentiment.finbert_local import score_texts

logger = logging.getLogger(__name__)
NEWS_PARQUET_DIR = "data/historical_news_parquet"


def _extract_stock_codes(text: str, valid_codes: set) -> list:
    """从新闻文本中提取股票代码（6 位数字）"""
    if not text:
        return []
    # 简化版：找所有 6 位数字
    candidates = re.findall(r"\b\d{6}\b", text)
    return [c for c in candidates if c in valid_codes]


def process_one_date(parquet_path: str, valid_codes: set) -> int:
    """处理单日 parquet → sentiment_history"""
    df = pd.read_parquet(parquet_path)
    if df.empty:
        return 0

    # 1. 给每条新闻打分（一次性 batch）
    texts = df["text"].fillna("").tolist()
    scores = score_texts(texts, batch_size=32)

    # 2. 给每条新闻匹配股票代码
    code_aggregates = defaultdict(list)  # code → [(score, source), ...]
    for i, row in df.iterrows():
        text = row["text"]
        score = scores[i]
        # 公告有 code，新闻需要从文本中匹配
        if row["code"]:
            matched_codes = [row["code"].split(".")[0]]  # 000001.SZ → 000001
        else:
            matched_codes = _extract_stock_codes(text, valid_codes)
        for code in matched_codes:
            code_aggregates[code].append((score, row["source"]))

    # 3. 按股票聚合（同一天同一股票的多条新闻取均值）
    date = os.path.basename(parquet_path).replace(".parquet", "")
    date_str = f"{date[:4]}-{date[4:6]}-{date[6:8]}"

    rows = []
    for code, items in code_aggregates.items():
        scores_only = [s for s, _ in items]
        sources = list(set(src for _, src in items))
        rows.append({
            "date": date_str,
            "code": code,
            "score": round(sum(scores_only) / len(scores_only), 4),
            "news_count": len(items),
            "sources": sources,
        })

    return save_sentiment_batch(rows)


def run_backfill(limit: int = 0):
    """全量回填历史情绪"""
    from data.storage import list_cached_stocks
    valid_codes = set(list_cached_stocks())

    files = sorted(f for f in os.listdir(NEWS_PARQUET_DIR) if f.endswith(".parquet"))
    if limit > 0:
        files = files[:limit]

    total_rows = 0
    for i, fname in enumerate(files, 1):
        path = os.path.join(NEWS_PARQUET_DIR, fname)
        try:
            n = process_one_date(path, valid_codes)
            total_rows += n
            if i % 50 == 0:
                logger.info(f"  [{i}/{len(files)}] 已写入 {total_rows} 行")
        except Exception as e:
            logger.warning(f"  {fname}: {e}")

    logger.info(f"回填完成: {total_rows} 条 sentiment_history 记录")
```

### C.4 集成到训练（1 天）

**修改 `ml/ranker.py:prepare_training_data`**：把 `sentiment_score = NaN` 改为从 sentiment_history JOIN：

```python
def prepare_training_data(...):
    ...
    # === 原 sentiment_score = NaN 处改造 ===
    # 从 sentiment_history 获取该截面日期的真实情绪分数
    factors["sentiment_score"] = _lookup_historical_sentiment(
        sym, end_date_str
    )
    ...


def _lookup_historical_sentiment(code: str, date: str) -> float:
    """优先内存缓存查询；未命中返回 NaN"""
    global _SENT_CACHE
    if "_SENT_CACHE" not in globals():
        # 一次性加载全部历史情绪到内存（~100MB 内）
        from data.sentiment_history import load_all_to_dict
        _SENT_CACHE = load_all_to_dict()
    return _SENT_CACHE.get((date, code), float("nan"))
```

`load_all_to_dict()` 一次 SQL：
```python
def load_all_to_dict() -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT date, code, score FROM sentiment_history")
    return {(d, c): s for d, c, s in cur.fetchall()}
```

### C.5 每日增量更新

**新建文件**: `scripts/sentiment_daily.py`

```python
"""
每日情绪增量更新（cron 调用）

每天 16:00 收盘后跑（与 evolve 错开）:
  1. 拉今日新闻（Tushare 当日）
  2. FinBERT 打分
  3. 入库 sentiment_history
"""
from datetime import datetime
from data.historical_news import fetch_news_by_date, fetch_announcements_by_date, _init_tushare
from data.sentiment_backfill import process_one_date
from data.sentiment_history import save_sentiment_batch


def run_daily():
    today = datetime.now().strftime("%Y%m%d")
    pro = _init_tushare()

    # 拉今日新闻
    anns = fetch_announcements_by_date(pro, today)
    news = fetch_news_by_date(pro, today, src="sina")
    # ... 处理流程同 historical_news.batch_fetch_to_parquet ...
    # 写入 sentiment_history


if __name__ == "__main__":
    run_daily()
```

**crontab 增加**:
```
# 每日收盘后情绪增量
30 16 * * 1-5 cd /home/ubuntu/pj_quant && python3 scripts/sentiment_daily.py >> logs/sentiment.log 2>&1
```

### C.6 验收

```bash
# 1. 历史新闻批量拉取（首次约 1.5 小时）
python3 -c "
from data.historical_news import batch_fetch_to_parquet
batch_fetch_to_parquet('20200101', '20260430')
"

# 2. 历史情绪批量打分（首次约 30 分钟，CPU）
python3 -c "from data.sentiment_backfill import run_backfill; run_backfill()"

# 3. 验证覆盖
python3 -c "
from data.sentiment_history import get_coverage
print(get_coverage())
"
# 预期: total_rows > 50000, unique_codes > 3000, unique_dates > 1400

# 4. 跑 evolve 验证情绪因子重要性
python3 main.py evolve
# 预期:
#   - feature_importance['sentiment_score'] > 0.02（vs 当前 0.0000）
#   - cv_r2_mean ≥ 0.08（情绪 + 中性化双重效果）
```

---

## 整体提交计划

按 6 个 commit 拆分（B 2 个，C 4 个）：

```
feat(sentiment): FinBERT-Chinese 本地推理替代 GLM (B.1+B.2)
- 新建 sentiment/finbert_local.py
- requirements.txt 加 transformers, torch
- 单元测试 tests/test_finbert.py
```

```
refactor(sentiment): flash_tag_sentiment 用 FinBERT 优先 (B.3)
- analyzer.flash_tag_sentiment: FinBERT → GLM → 规则三级降级
- _batch_sentiment_factors 改为 FinBERT 一次性 batch
- 性能: GLM 32min → FinBERT 5min
```

```
feat(data): sentiment_history SQLite 表 + helper (C.1)
- data/sentiment_history.py 新建
- save/load/coverage 接口
```

```
feat(data): 历史新闻批量拉取 (C.2)
- data/historical_news.py 新建
- Tushare anns_d + news 接口
- batch_fetch_to_parquet 入口
```

```
feat(data): 历史情绪批量回填 + 文本-代码匹配 (C.3)
- data/sentiment_backfill.py 新建
- run_backfill 入口
```

```
feat(ml): 训练流程集成 sentiment_history JOIN (C.4+C.5)
- ml/ranker.prepare_training_data 改用 _lookup_historical_sentiment
- 全局缓存避免重复 IO
- scripts/sentiment_daily.py 每日增量入口
- crontab 配置说明
```

每个 commit 末尾：
```
Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
```

---

## 实施时间表

| 阶段 | 任务 | 工时 | 实施触发条件 |
|------|------|------|-------------|
| **Stage B**（短期）| FinBERT 替代 + 测试 | 1-2 天 | GLM 限流问题持续 |
| Stage C.1-C.2 | 数据库 + 历史新闻拉取 | 2 天 | B 完成后 |
| Stage C.3-C.4 | 批量回填 + 训练集成 | 2-3 天 | C.2 完成后 |
| Stage C.5 | 每日增量 cron | 1 天 | C.4 验收通过 |

每个 Stage 独立 commit，单独 revert。

---

## 风险与回退

| 风险 | 缓解 |
|------|------|
| FinBERT 模型下载失败 | requirements 不强制 torch；is_available() 失败时降级 GLM |
| FinBERT 推理慢于预期 | 改用 onnxruntime-cpu（量化模型，3-5x 加速） |
| Tushare news 接口需要更高积分 | 改用东方财富 + 新浪历史抓取（更慢但免费） |
| 历史新闻覆盖不足某些股票 | 没情绪的股票 sentiment_score=NaN，不影响其他特征 |
| FinBERT 中文情感分类质量不达预期 | 切换到 Erlangshen-RoBERTa（IDEA 团队，准确率 75%）|
| sentiment_history 表过大（千万行） | 加 date+code 联合主键和索引；按年分表 |

---

## 不在本次范围

- 微博/股吧/雪球等社交媒体情绪（C.2 之后单独项目）
- LLM 推理优化为 onnx/quant 版本（FinBERT 慢时再做）
- 多语言金融情绪（仅中文 A 股）
- 实时流式情绪（与本批处理架构正交）

---

## 预期收益

| 指标 | Stage B 后 | Stage C 后 |
|------|-----------|------------|
| evolve 时间 | 95min → 65min | 不变 |
| 情绪 API 限流 | 几乎消除 | 完全消除 |
| 情绪因子 importance | 0% (训练时仍 NaN) | **>2%（业界水平）** |
| cv_r2_mean | 仍受中性化主导 ~0.07 | **0.08-0.12**（情绪+中性化双增益）|
| 推送中"利好"维度 | FinBERT 实时打分 | 历史可比 + 实时打分 |
