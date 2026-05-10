"""前向 OOS 累积工具

记录每周一推送的 D 共识 picks，T+5 后自动评估实际收益，累积到 80+ 周
让统计显著性 (DSR ≥ 0.95) 达标，再决定是否上真金白银。

数据流:
  周一 08:30  run_weekly.sh → consensus picks → record_picks() 写入
              picks_history 表 (eval_status=pending)
  Tue-Fri     run_daily.sh → evaluate_pending() 扫描 5d 已满的 picks
              算 ret_5d / bench_5d / alpha 回填 (status=evaluated)
  月初/周末    monthly_report() 累积报告: 真实 alpha vs 回测预期 / 样本进度
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

DB_PATH = "data/quant.db"
TABLE = "picks_history"
HOLD_DAYS = 5  # 与生产 D 方案一致


def _init_table(conn: sqlite3.Connection) -> None:
    """建表 + 索引，幂等"""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            pick_date     TEXT,
            code          TEXT,
            pick_top_n    INTEGER,
            rank          INTEGER,
            freq          INTEGER,
            avg_score     REAL,
            is_st         INTEGER,
            eval_date     TEXT,
            p0            REAL,
            p1            REAL,
            ret_5d        REAL,
            bench_5d      REAL,
            alpha         REAL,
            eval_status   TEXT NOT NULL DEFAULT 'pending',
            created_at    TEXT NOT NULL,
            evaluated_at  TEXT,
            PRIMARY KEY (pick_date, code, pick_top_n)
        )
    """)
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_ph_status ON {TABLE}(eval_status)")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_ph_date ON {TABLE}(pick_date)")
    conn.commit()


def _get_conn() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def _is_st(conn: sqlite3.Connection, code: str) -> int:
    row = conn.execute(
        "SELECT name FROM industry_map WHERE code=?", (code,)
    ).fetchone()
    if not row or not row[0]:
        return 0
    return 1 if "ST" in row[0] else 0


def record_picks(pick_date: str, picks: list[dict], pick_top_n: int) -> int:
    """记录一次推送的 picks (eval_status=pending)

    Parameters
    ----------
    pick_date : YYYY-MM-DD 推送日
    picks : list of {code, freq, avg_score, ...}, 已按 rank 排序
    pick_top_n : 本次推送的 N 值 (D_top3=3 / D_top10=10 等)

    Returns
    -------
    int : 入库行数 (INSERT OR REPLACE 幂等)
    """
    if not picks:
        return 0
    conn = _get_conn()
    try:
        _init_table(conn)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        rows = []
        for rank, p in enumerate(picks[:pick_top_n], 1):
            code = str(p["code"])
            rows.append((
                pick_date, code, pick_top_n, rank,
                int(p.get("freq", 0)), float(p.get("avg_score", 0)),
                _is_st(conn, code),
                None, None, None, None, None, None,  # eval fields
                "pending", now, None,
            ))
        conn.executemany(
            f"INSERT OR REPLACE INTO {TABLE} VALUES "
            f"({','.join(['?'] * 16)})", rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _trading_days_after(start: str, n: int) -> Optional[str]:
    """返回 start 之后第 n 个交易日（用 chinese_calendar 排假期）"""
    try:
        import chinese_calendar
        d = pd.Timestamp(start).date() + timedelta(days=1)
        seen = 0
        while seen < n:
            if chinese_calendar.is_workday(d) and d.weekday() < 5:
                seen += 1
                if seen == n:
                    return d.strftime("%Y-%m-%d")
            d += timedelta(days=1)
            if (d - pd.Timestamp(start).date()).days > 30:  # 兜底
                return None
        return None
    except (ImportError, NotImplementedError):
        return (pd.Timestamp(start) + pd.Timedelta(days=int(n * 1.4) + 1)).strftime("%Y-%m-%d")


def _stock_close_on(conn: sqlite3.Connection, code: str, on: str) -> Optional[float]:
    """取 stock_{code} 表中 date <= on 当天结束 的最后一个 close

    注意 stock 表 date 类型是 TIMESTAMP (存为 'YYYY-MM-DD 00:00:00')，
    SQLite 字符串比较时 'YYYY-MM-DD' < 'YYYY-MM-DD 00:00:00'，所以 on 参数
    必须扩到当天 23:59:59 才能包含 on 当日的 bar。
    """
    try:
        from factors.data_loader import _safe_table_name
        table = _safe_table_name(code)
    except (ValueError, ImportError):
        return None
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone():
        return None
    row = conn.execute(
        f"SELECT close FROM {table} WHERE date <= ? ORDER BY date DESC LIMIT 1",
        (on + " 23:59:59",),
    ).fetchone()
    return float(row[0]) if row and row[0] else None


def _compute_bench_5d(conn: sqlite3.Connection, pick_date: str, eval_date: str,
                     min_cap: float = 5e8, max_cap: float = 5e9) -> Optional[float]:
    """同期池 (5e8-5e9) 等权 5d 收益 (与 backtest_year 一致)"""
    try:
        from factors.data_loader import get_small_cap_stocks
    except ImportError:
        return None
    pool = get_small_cap_stocks(min_cap, max_cap)
    rets = []
    for c in pool["code"].tolist():
        p0 = _stock_close_on(conn, c, pick_date)
        p1 = _stock_close_on(conn, c, eval_date)
        if p0 and p1 and p0 > 0:
            rets.append(p1 / p0 - 1)
    return float(sum(rets) / len(rets)) if rets else None


def evaluate_pending(today: Optional[str] = None) -> dict:
    """扫所有 pending picks，对 5d 已满的算实际收益回填

    Returns
    -------
    dict : {evaluated, skipped, failed}
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    conn = _get_conn()
    try:
        _init_table(conn)
        pending = conn.execute(
            f"SELECT pick_date, code, pick_top_n FROM {TABLE} "
            f"WHERE eval_status='pending'"
        ).fetchall()
        if not pending:
            return {"evaluated": 0, "skipped": 0, "failed": 0}

        bench_cache: dict[tuple[str, str], Optional[float]] = {}
        evaluated = skipped = failed = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for pick_date, code, top_n in pending:
            eval_date = _trading_days_after(pick_date, HOLD_DAYS)
            if not eval_date or eval_date > today:
                skipped += 1
                continue

            p0 = _stock_close_on(conn, code, pick_date)
            p1 = _stock_close_on(conn, code, eval_date)
            if p0 is None or p1 is None or p0 <= 0:
                conn.execute(
                    f"UPDATE {TABLE} SET eval_status='failed', evaluated_at=? "
                    f"WHERE pick_date=? AND code=? AND pick_top_n=?",
                    (now, pick_date, code, top_n),
                )
                failed += 1
                continue
            ret = p1 / p0 - 1

            key = (pick_date, eval_date)
            if key not in bench_cache:
                bench_cache[key] = _compute_bench_5d(conn, pick_date, eval_date)
            bench = bench_cache[key]
            alpha = ret - bench if bench is not None else None

            conn.execute(
                f"UPDATE {TABLE} SET eval_date=?, p0=?, p1=?, ret_5d=?, "
                f"bench_5d=?, alpha=?, eval_status='evaluated', evaluated_at=? "
                f"WHERE pick_date=? AND code=? AND pick_top_n=?",
                (eval_date, p0, p1, ret, bench, alpha, now,
                 pick_date, code, top_n),
            )
            evaluated += 1

        conn.commit()
        if evaluated or failed:
            logger.info(
                f"evaluate_pending: {evaluated} 已评估, {skipped} 待 5d 满, {failed} 失败"
            )
        return {"evaluated": evaluated, "skipped": skipped, "failed": failed}
    finally:
        conn.close()


def get_stats() -> dict:
    """当前累积状态摘要"""
    conn = _get_conn()
    try:
        _init_table(conn)
        rows = conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT pick_date), "
            f"SUM(CASE WHEN eval_status='evaluated' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN eval_status='pending' THEN 1 ELSE 0 END), "
            f"MIN(pick_date), MAX(pick_date) "
            f"FROM {TABLE}"
        ).fetchone()
        total, weeks, evaluated, pending, mn, mx = rows
        return {
            "total_picks": total or 0,
            "n_weeks": weeks or 0,
            "evaluated": evaluated or 0,
            "pending": pending or 0,
            "min_date": mn,
            "max_date": mx,
            "weeks_to_significance": max(0, 80 - (weeks or 0)),
        }
    finally:
        conn.close()


def load_evaluated(pick_top_n: Optional[int] = None,
                   exclude_st: bool = False) -> pd.DataFrame:
    """加载已评估 picks 用于报告"""
    conn = _get_conn()
    try:
        _init_table(conn)
        sql = (f"SELECT * FROM {TABLE} WHERE eval_status='evaluated'")
        params: list = []
        if pick_top_n is not None:
            sql += " AND pick_top_n=?"
            params.append(pick_top_n)
        if exclude_st:
            sql += " AND is_st=0"
        return pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()


def _stats_for_subset(sub: pd.DataFrame) -> dict:
    """单个子集的 (胜率 / avg α / 累计 α) 三件套"""
    if sub.empty:
        return {"n": 0, "win_rate": 0.0, "avg_alpha": 0.0, "cum_alpha": 0.0}
    wr = (sub["ret_5d"] > 0).mean()
    avg_a = sub["alpha"].mean()
    weekly = sub.groupby("pick_date").agg(
        avg_ret=("ret_5d", "mean"), bench=("bench_5d", "first"),
    )
    weekly["alpha"] = weekly["avg_ret"] - weekly["bench"]
    cum_a = ((1 + weekly["alpha"]).cumprod() - 1).iloc[-1] if len(weekly) else 0
    return {"n": len(sub), "win_rate": float(wr),
            "avg_alpha": float(avg_a), "cum_alpha": float(cum_a)}


def monthly_report() -> str:
    """累积月报 markdown (大白话风格)

    - 主表: 生产实际 (NUM_POSITIONS=10) 推送的真实表现
    - hypothetical 副表: 用 rank ≤ N 切片做"如果只买前 N 只"
    - 跟回测预期对比
    - 给一句行动建议
    """
    stats = get_stats()
    df = load_evaluated()
    n_weeks = stats["n_weeks"]
    weeks_left = stats["weeks_to_significance"]
    months_left = weeks_left / 4
    is_empty = stats["total_picks"] == 0

    lines = [
        f"# 模型推荐验证月报 · {datetime.now().strftime('%Y-%m-%d')}",
        "",
        "## 术语速查",
        "",
        "- **picks**: 每周一推送的 10 只股票（生产 NUM_POSITIONS=10）",
        "- **5d 收益**: 周一开盘买入，5 个交易日后卖出的涨跌幅",
        "- **基准 (bench)**: 同期 5-50 亿小盘股池约 1700 只等权 5d 收益（"
        "我们策略对应的「市场平均」）",
        "- **α (alpha)**: 我们 10 只 picks 等权 5d 收益 − 同期基准 5d 收益。"
        "α > 0 跑赢市场，α < 0 跑输",
        "- **pp**: 百分点（percentage point）。+1pp/只 = 每只股 5 天比基准多赚 1%",
        "- **累计 α**: 每周 α 按复利累乘，看长期是否稳定跑赢",
        "- **胜率**: picks 5d 收益为正的占比（不是跑赢基准的占比）",
        "",
        "## 已积累多久？",
        "",
        f"- 数据范围: {stats['min_date'] or '尚未开始'} ~ {stats['max_date'] or '尚未开始'}",
        f"- 已推送 **{n_weeks} 周** / 累积 picks: **{stats['total_picks']}** "
        f"(已评估 {stats['evaluated']} / 5d hold 未满待评估 {stats['pending']})",
        "",
        f"> 业界经验: 要让结论「统计上靠谱」，至少需要 **80 周** (≈ 1.5 年)。",
        f"> 现在还差 **{weeks_left} 周** (约 {months_left:.1f} 个月)。",
        "",
    ]
    if is_empty:
        lines += [
            "## 表现评估",
            "",
            "_尚未有真实推送数据。生产 cron 每周一推送后会自动 record，"
            "5 个交易日后自动评估。第一份有意义的报告需等到首次推送 5 天后。_",
            "",
            "## 我现在该做什么？",
            "",
            "- 等待生产 cron 第一次推送（周一 08:30）",
            "- 5 个交易日后会有第一组评估数据",
            "- 后续每周自动累积，目标 80 周达统计显著",
        ]
        return "\n".join(lines)

    if df.empty:
        lines.append("\n## 暂无评估数据 (5d hold 未满)")
        return "\n".join(lines)

    # 主表: 生产实际推送 (NUM_POSITIONS=10)
    prod_top_n = max(df["pick_top_n"].unique())  # 生产实际 = 最大 top_n
    prod = df[df["pick_top_n"] == prod_top_n]
    prod_inc = _stats_for_subset(prod)
    prod_exc = _stats_for_subset(prod[prod["is_st"] == 0])

    lines += [
        f"## 14 周里这 {prod_top_n} 只股表现如何（生产实际推送）",
        "",
        f"|  | 含 ST | 排 ST |",
        f"|---|---:|---:|",
        f"| picks 数 | {prod_inc['n']} | {prod_exc['n']} |",
        f"| 胜率（5 天涨跌为正） | **{prod_inc['win_rate'] * 100:.1f}%** | "
        f"{prod_exc['win_rate'] * 100:.1f}% |",
        f"| 平均跑赢同期池子 | {prod_inc['avg_alpha'] * 100:+.2f} pp/只 | "
        f"{prod_exc['avg_alpha'] * 100:+.2f} pp/只 |",
        f"| 14 周累计跑赢 | **{prod_inc['cum_alpha'] * 100:+.2f}%** | "
        f"**{prod_exc['cum_alpha'] * 100:+.2f}%** |",
        "",
        f"> **解读**: 50% 是硬币概率；swing 策略 45-65% 算合理。"
        f"含 ST 数字若高于排 ST，说明 alpha 主要来自 ST 高波动反弹。",
        "",
    ]

    # 副表: hypothetical "如果你只买前 N 只"
    lines += [
        "## 如果你只买排名最高的 N 只会怎样（hypothetical）",
        "",
        "rank 1 = 当周共识分数最高的那只。下表是从生产 picks 中切片的事后统计，",
        "**不是生产推送策略**；只供观察"
        "「如果只挑前几只是不是更好」。",
        "",
        "| 切片 |  | picks | 胜率 | avg α | 累计 α |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for hyp_n in (3, 5):
        if hyp_n >= prod_top_n:
            continue
        sliced = prod[prod["rank"] <= hyp_n]
        for label, sub in [
            ("含 ST", sliced),
            ("排 ST", sliced[sliced["is_st"] == 0]),
        ]:
            s = _stats_for_subset(sub)
            lines.append(
                f"| rank 1-{hyp_n} | {label} | {s['n']} | "
                f"{s['win_rate'] * 100:.1f}% | {s['avg_alpha'] * 100:+.2f} pp | "
                f"{s['cum_alpha'] * 100:+.2f}% |"
            )

    # 跟回测对比
    lines += [
        "",
        "## 跟回测预期对比",
        "",
        "回测 (2026-01-05 ~ 04-27, 单段牛市):",
        "- D_top10 含 ST 累计 α: 回测预期 +11.14%",
        "- D_top10 排 ST 累计 α: 回测预期 +0.37%",
        "",
        f"实际 D_top{prod_top_n}:",
        f"- 含 ST 累计 α: **{prod_inc['cum_alpha'] * 100:+.2f}%**",
        f"- 排 ST 累计 α: **{prod_exc['cum_alpha'] * 100:+.2f}%**",
        "",
    ]
    if prod_exc["cum_alpha"] < -0.05:
        verdict = "⚠️ 排 ST 累计明显为负 — 模型主要 alpha 是 ST 反弹，可在实盘失效"
    elif prod_exc["cum_alpha"] > 0.05:
        verdict = "✅ 排 ST 仍稳定为正 — 模型有真 alpha，可考虑下一阶段"
    else:
        verdict = "⏸ 排 ST 累计接近 0 — 数据不足以下定论，继续累积"
    lines.append(f"**初步判断**: {verdict}")
    lines.append("")

    # 行动建议
    lines += [
        "## 我现在该做什么？",
        "",
        f"- 继续累积 {months_left:.0f} 个月直到样本足够 ({weeks_left} 周后再正式判断)",
        "- 现阶段**不上真金白银**",
        "- 关注**排 ST 累计 α 趋势** (这是真 alpha 指标，含 ST 数字会被反弹机噪声放大)",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "stats"
    if cmd == "stats":
        s = get_stats()
        print(f"weeks={s['n_weeks']} / picks={s['total_picks']}")
        print(f"evaluated={s['evaluated']} / pending={s['pending']}")
        print(f"to_significance={s['weeks_to_significance']} 周")
    elif cmd == "evaluate":
        print(evaluate_pending())
    elif cmd == "report":
        print(monthly_report())
    else:
        print(f"未知命令: {cmd}; 支持 stats / evaluate / report")
