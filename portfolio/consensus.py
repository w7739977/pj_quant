"""5 天频次共识选股（D 方案）

策略逻辑
--------
1. 每个交易日选股时，把当日 top 10（按 final_score=0.5×ML+0.5×因子）入 SQLite 缓存
2. 共识选股日（默认周一）取过去 N 个交易日 top 10 的并集
3. 按"出现次数 + 平均得分"降序排，取 top N

实证（2026-01-01 ~ 04-23 共 13 周回测）
---------------------------------------
方案对比 (avg_alpha / 累计 / sharpe-like / max_dd):
  A. 日频基线           +0.41% / —      / +0.15 / -5.40%
  B. 周一快照           +0.04% / +0.01% / +0.01 / -5.40%
  C. 5 天信号平均        +0.68% / +8.87% / +0.28 / -3.59%
  D. 5 天频次共识 (本)   +1.15% / +15.69%/ +0.50 / -2.20% ⭐

业界对应
--------
- "Persistent signal filter" / "signal stability bonus"
- AQR / Two Sigma 等机构常用的"top-quintile 共现频次过滤"
"""
import os
import sqlite3
import logging
from collections import Counter
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

DB_PATH = "data/quant.db"
TABLE = "daily_scored_cache"

# 退市/停牌守卫：last bar 距目标日超过此 *交易日数* 视为非活跃。
# 5 = 一周工作日，足以容忍周末 + 调休短假；用 chinese_calendar 排除节假日，
# 春节 / 国庆等连休下节后第一周的真活跃股不会被误剔除（自然日基线会误杀）。
MAX_STALE_DAYS = 5


def is_window_fresh(last_bar, target, max_gap_days: int = MAX_STALE_DAYS) -> bool:
    """窗口最末 bar 距 target 不超过 max_gap_days 个 *交易日*。

    用 chinese_calendar 排除周末 + 节假日。chinese_calendar 不覆盖目标年份
    时回退到自然日（max_gap_days * 7 // 5 + 1，5 交易日 ≈ 8 自然日）。

    用于 backfill / 回测的 DataFrame 路径（`win.iloc[-1]["date_str"]`）；
    SQL 路径见 `_is_active`。
    """
    last_dt = pd.Timestamp(last_bar).date()
    target_dt = pd.Timestamp(target).date()
    if last_dt >= target_dt:
        return True
    try:
        import chinese_calendar
        gap = len(chinese_calendar.get_workdays(last_dt + timedelta(days=1), target_dt))
    except (NotImplementedError, ImportError):
        return (target_dt - last_dt).days <= max_gap_days * 7 // 5 + 1
    return gap <= max_gap_days


def _init_table(conn: sqlite3.Connection) -> None:
    """Create daily_scored_cache table if not exists."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            date TEXT,
            code TEXT,
            final_score REAL,
            top_n_rank INTEGER,
            updated_at TEXT,
            PRIMARY KEY (date, code)
        )
    """)
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_dsc_date ON {TABLE}(date)")
    conn.commit()


def _is_active(conn: sqlite3.Connection, code: str, date: str,
               max_gap_days: int = MAX_STALE_DAYS) -> bool:
    """SQL 版活跃度检查：兜底用，阻止 cache_scored 写入退市/停牌股。

    backfill 主路径已加新鲜度守卫；这里是双保险。
    """
    from factors.data_loader import _safe_table_name
    try:
        table = _safe_table_name(code)
    except ValueError:
        return False
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return False
    row = conn.execute(
        f"SELECT MAX(date) FROM {table} WHERE date <= ?", (date,)
    ).fetchone()
    if not row or not row[0]:
        return False
    return is_window_fresh(str(row[0])[:10], date, max_gap_days)


def cache_scored(date: str, scored_df: pd.DataFrame, top_n: int = 10) -> int:
    """
    缓存当日 top N 的 final_score（节省空间，共识只关心 top）

    Parameters
    ----------
    date : YYYY-MM-DD
    scored_df : 含 'code' 和 'final_score' 列
    top_n : 缓存前 N 名，默认 10

    Returns
    -------
    int : 入库行数
    """
    if scored_df.empty or "code" not in scored_df.columns or "final_score" not in scored_df.columns:
        logger.warning(f"cache_scored {date}: scored_df 为空或缺列")
        return 0

    sub = scored_df.sort_values("final_score", ascending=False).head(top_n).copy()
    sub["rank"] = range(1, len(sub) + 1)

    conn = sqlite3.connect(DB_PATH)
    try:
        _init_table(conn)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        skipped = []
        for _, row in sub.iterrows():
            code = str(row["code"])
            if not _is_active(conn, code, date):
                skipped.append(code)
                continue
            rows.append((date, code, float(row["final_score"]), int(row["rank"]), now))
        if skipped:
            sample = skipped[:10]
            more = f" (+{len(skipped) - 10} more)" if len(skipped) > 10 else ""
            logger.warning(f"cache_scored {date}: 跳过 {len(skipped)} 只非活跃股 {sample}{more}")
        if not rows:
            return 0
        conn.executemany(
            f"INSERT OR REPLACE INTO {TABLE} "
            f"(date, code, final_score, top_n_rank, updated_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def load_recent_scored(end_date: str, window: int = 5) -> dict:
    """
    加载 end_date 之前 window 个不同日期的 scored 缓存

    Parameters
    ----------
    end_date : YYYY-MM-DD（不含）
    window : 取最近多少个交易日（按 cache 中存在的日期）

    Returns
    -------
    dict : {date_str: [(code, final_score, rank), ...]}
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        _init_table(conn)
        cur = conn.execute(
            f"SELECT DISTINCT date FROM {TABLE} WHERE date < ? ORDER BY date DESC LIMIT ?",
            (end_date, window),
        )
        recent_dates = [r[0] for r in cur.fetchall()]
        if not recent_dates:
            return {}
        result = {}
        for d in recent_dates:
            cur = conn.execute(
                f"SELECT code, final_score, top_n_rank FROM {TABLE} "
                f"WHERE date = ? ORDER BY top_n_rank ASC",
                (d,),
            )
            result[d] = [(c, s, r) for c, s, r in cur.fetchall()]
        return result
    finally:
        conn.close()


def consensus_picks(end_date: str, window: int = 5, top_n: int = 10) -> list:
    """
    频次共识选股：基于 end_date 之前 window 个交易日的 top 10 缓存

    排序规则:
      1. 出现次数（top 10 之内）降序
      2. 平均得分降序（tiebreaker）

    Returns
    -------
    list[dict] : [{code, freq, avg_score, days_available}]
    """
    history = load_recent_scored(end_date, window)
    if not history:
        logger.warning(f"consensus_picks: 无缓存数据 (end_date={end_date})")
        return []

    counter = Counter()
    score_sum: dict[str, list[float]] = {}
    for d, rows in history.items():
        for code, score, _ in rows:
            counter[code] += 1
            score_sum.setdefault(code, []).append(score)

    days_available = len(history)
    if days_available < window:
        logger.warning(
            f"共识选股: cache 仅 {days_available}/{window} 天数据 "
            f"(日期 {sorted(history.keys())})，结果稳定性下降"
        )

    ranked = sorted(
        counter.items(),
        key=lambda x: (-x[1], -float(np.mean(score_sum[x[0]]))),
    )

    return [
        {
            "code": code,
            "freq": counter[code],
            "avg_score": float(np.mean(score_sum[code])),
            "days_available": days_available,
        }
        for code, _ in ranked[:top_n]
    ]


def cache_stats() -> dict:
    """缓存统计（监控用）"""
    conn = sqlite3.connect(DB_PATH)
    try:
        _init_table(conn)
        row = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT date), MIN(date), MAX(date) FROM {TABLE}"
        ).fetchone()
        return {
            "total_rows": row[0],
            "distinct_dates": row[1],
            "min_date": row[2],
            "max_date": row[3],
        }
    finally:
        conn.close()
