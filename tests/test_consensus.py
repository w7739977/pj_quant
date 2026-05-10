"""5 天频次共识选股（D 方案）单测"""
import os
import sqlite3
import tempfile

import pandas as pd
import pytest

from portfolio import consensus


@pytest.fixture
def temp_db(monkeypatch):
    """每个测试用独立临时 SQLite。

    默认 patch `_is_active` 始终通过：cache 写入 / consensus 排序的旧测试
    与 stock_{code} 表是否存在无关；活跃度过滤的语义在 `test_is_active_*`
    单独覆盖。
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(consensus, "DB_PATH", path)
    monkeypatch.setattr(consensus, "_is_active", lambda *a, **kw: True)
    yield path
    os.unlink(path)


def _make_stock_table(conn: sqlite3.Connection, code: str, dates: list[str]) -> None:
    conn.execute(f"CREATE TABLE stock_{code} (date TIMESTAMP, close REAL)")
    for d in dates:
        conn.execute(f"INSERT INTO stock_{code} VALUES (?, 10.0)", (d,))
    conn.commit()


def _make_scored(codes_scores):
    return pd.DataFrame([
        {"code": c, "final_score": s} for c, s in codes_scores
    ])


def test_cache_scored_writes_top_n(temp_db):
    """只缓存 top N，多余被丢弃"""
    df = _make_scored([("000001", 1.0), ("000002", 0.5), ("000003", 0.1), ("000004", -0.5)])
    n = consensus.cache_scored("2026-04-01", df, top_n=2)
    assert n == 2
    history = consensus.load_recent_scored("2026-04-02", window=5)
    assert "2026-04-01" in history
    rows = history["2026-04-01"]
    assert len(rows) == 2
    assert rows[0][0] == "000001"  # rank 1
    assert rows[1][0] == "000002"  # rank 2


def test_cache_scored_empty_safe(temp_db):
    """空 df 不报错"""
    n = consensus.cache_scored("2026-04-01", pd.DataFrame(), top_n=5)
    assert n == 0


def test_cache_scored_idempotent(temp_db):
    """同一 (date, code) 再次写入会 REPLACE"""
    consensus.cache_scored("2026-04-01", _make_scored([("000001", 1.0)]), top_n=10)
    consensus.cache_scored("2026-04-01", _make_scored([("000001", 2.0)]), top_n=10)
    history = consensus.load_recent_scored("2026-04-02", window=5)
    assert history["2026-04-01"][0][1] == 2.0  # 后写覆盖前写


def test_load_recent_scored_window(temp_db):
    """只取 end_date 之前最近 N 天"""
    for d, score in [("2026-03-25", 1.0), ("2026-03-26", 1.1), ("2026-03-27", 1.2),
                     ("2026-03-30", 1.3), ("2026-03-31", 1.4), ("2026-04-01", 1.5)]:
        consensus.cache_scored(d, _make_scored([("000001", score)]), top_n=10)
    history = consensus.load_recent_scored("2026-04-01", window=3)
    assert sorted(history.keys()) == ["2026-03-27", "2026-03-30", "2026-03-31"]


def test_load_recent_scored_excludes_end_date(temp_db):
    """end_date 当天不计入（仅取严格之前）"""
    consensus.cache_scored("2026-04-01", _make_scored([("000001", 1.0)]), top_n=10)
    history = consensus.load_recent_scored("2026-04-01", window=5)
    assert history == {}


def test_consensus_picks_frequency_priority(temp_db):
    """高频次股票优先于高得分单日股"""
    # 5 天，A 出现 5 次（每天分 0.5），B 仅 1 次但分 99.0
    for d in ["2026-03-24", "2026-03-25", "2026-03-26", "2026-03-27", "2026-03-30"]:
        consensus.cache_scored(d, _make_scored([("A", 0.5)]), top_n=10)
    consensus.cache_scored("2026-03-30", _make_scored([("A", 0.5), ("B", 99.0)]), top_n=10)
    picks = consensus.consensus_picks("2026-03-31", window=5, top_n=2)
    codes = [p["code"] for p in picks]
    assert codes[0] == "A"  # 高频次优先
    # B 只出现 1 次，应排在 A 后；其他股不存在的话只有 A、B
    assert "B" in codes


def test_consensus_picks_tiebreaker_by_avg_score(temp_db):
    """同频次时按平均得分排"""
    for d in ["2026-03-26", "2026-03-27", "2026-03-30"]:
        consensus.cache_scored(d, _make_scored([("LOW", 0.1), ("HI", 1.0)]), top_n=10)
    picks = consensus.consensus_picks("2026-03-31", window=3, top_n=2)
    assert picks[0]["code"] == "HI"
    assert picks[1]["code"] == "LOW"
    assert picks[0]["freq"] == 3
    assert picks[1]["freq"] == 3


def test_consensus_picks_empty_cache(temp_db):
    """空缓存返回空列表，不抛异常"""
    picks = consensus.consensus_picks("2026-04-01", window=5, top_n=10)
    assert picks == []


def test_consensus_picks_partial_cache(temp_db):
    """缓存不足 window 天仍返回结果，但带 days_available 告警"""
    consensus.cache_scored("2026-03-30", _make_scored([("X", 1.0)]), top_n=10)
    consensus.cache_scored("2026-03-31", _make_scored([("X", 0.9)]), top_n=10)
    picks = consensus.consensus_picks("2026-04-01", window=5, top_n=10)
    assert len(picks) == 1
    assert picks[0]["code"] == "X"
    assert picks[0]["freq"] == 2
    assert picks[0]["days_available"] == 2  # 实际可用天数


def test_consensus_picks_avg_score_correct(temp_db):
    """平均得分计算正确"""
    consensus.cache_scored("2026-03-30", _make_scored([("X", 1.0)]), top_n=10)
    consensus.cache_scored("2026-03-31", _make_scored([("X", 3.0)]), top_n=10)
    picks = consensus.consensus_picks("2026-04-01", window=5, top_n=10)
    assert picks[0]["avg_score"] == pytest.approx(2.0)


def test_cache_stats(temp_db):
    """统计接口"""
    consensus.cache_scored("2026-03-30", _make_scored([("A", 1), ("B", 2)]), top_n=10)
    consensus.cache_scored("2026-03-31", _make_scored([("A", 1)]), top_n=10)
    stats = consensus.cache_stats()
    assert stats["total_rows"] == 3
    assert stats["distinct_dates"] == 2
    assert stats["min_date"] == "2026-03-30"
    assert stats["max_date"] == "2026-03-31"


# ============ 新鲜度守卫 — is_window_fresh (交易日基线) ============

def test_is_window_fresh_same_day():
    """target == last_bar，gap=0，新鲜"""
    assert consensus.is_window_fresh("2026-04-30", "2026-04-30") is True


def test_is_window_fresh_at_threshold():
    """5 个交易日 (4-24, 4-27, 4-28, 4-29, 4-30) 内仍视为新鲜（边界）"""
    # 2026-04-23(周四) → 2026-04-30(周四): get_workdays(4-24, 4-30) = 5 个
    assert consensus.is_window_fresh("2026-04-23", "2026-04-30") is True


def test_is_window_fresh_just_over():
    """6 个交易日已超 5 阈值，非新鲜"""
    # 2026-04-22(周三) → 2026-04-30(周四): get_workdays(4-23, 4-30) = 6 个
    assert consensus.is_window_fresh("2026-04-22", "2026-04-30") is False


def test_is_window_fresh_skips_weekend():
    """周末不计入交易日数 — last_bar 周一，target 同周五，gap=4 个交易日"""
    # 2026-04-27(周一) → 2026-05-01: get_workdays(4-28, 5-1) = 4-28, 4-29, 4-30 = 3 (5-1 劳动节)
    assert consensus.is_window_fresh("2026-04-27", "2026-05-01") is True


def test_is_window_fresh_skips_holiday_labour():
    """劳动节 5 天连休：节前 last_bar 节后查仍新鲜（自然日 9 天）"""
    # 2026-04-30(周四) → 2026-05-08(周五): 自然日 8 天，但只 3 个交易日 (5-6/7/8)
    assert consensus.is_window_fresh("2026-04-30", "2026-05-08") is True


def test_is_window_fresh_skips_holiday_spring():
    """春节连休：自然日 7 天阈值会误杀的场景，交易日基线下保留"""
    # 2026-02-13(周五，春节前最后交易日) → 2026-02-24(春节后首个交易日)
    # 自然日 11 天 > 7（旧逻辑误杀）；交易日 = get_workdays(2-14, 2-24) = 仅 2-24 = 1 个
    assert consensus.is_window_fresh("2026-02-13", "2026-02-24") is True


def test_is_window_fresh_custom_threshold():
    """自定义 max_gap_days 生效"""
    # 2026-04-27(周一) → 2026-04-30(周四): 3 个交易日 (4-28/29/30)
    assert consensus.is_window_fresh("2026-04-27", "2026-04-30", max_gap_days=3) is True
    assert consensus.is_window_fresh("2026-04-27", "2026-04-30", max_gap_days=2) is False


def test_is_window_fresh_accepts_timestamp_target():
    """target 可以是 pd.Timestamp（hoist 优化路径）"""
    assert consensus.is_window_fresh("2026-04-30", pd.Timestamp("2026-04-30")) is True


# ============ 活跃度检查 — _is_active (SQL 路径) ============

def _setup_active_db(monkeypatch):
    """生成一个干净的临时 db 并连上，返回 (conn, path)"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(consensus, "DB_PATH", path)
    return sqlite3.connect(path), path


def test_is_active_active_stock(monkeypatch):
    """有近期 bar 的股票视为活跃"""
    conn, path = _setup_active_db(monkeypatch)
    try:
        _make_stock_table(conn, "000001", ["2026-04-29"])
        assert consensus._is_active(conn, "000001", "2026-04-30") is True
    finally:
        conn.close()
        os.unlink(path)


def test_is_active_delisted_stock(monkeypatch):
    """陈旧 bar 距 target 超 7 天视为非活跃"""
    conn, path = _setup_active_db(monkeypatch)
    try:
        _make_stock_table(conn, "002619", ["2022-03-31"])
        assert consensus._is_active(conn, "002619", "2026-04-17") is False
    finally:
        conn.close()
        os.unlink(path)


def test_is_active_table_missing(monkeypatch):
    """stock_{code} 表不存在视为非活跃（不抛异常）"""
    conn, path = _setup_active_db(monkeypatch)
    try:
        assert consensus._is_active(conn, "999999", "2026-04-30") is False
    finally:
        conn.close()
        os.unlink(path)


def test_is_active_invalid_code_blocks_injection(monkeypatch):
    """非 6 位数字 code 被 _safe_table_name 挡住，返回 False（不抛）"""
    conn, path = _setup_active_db(monkeypatch)
    try:
        assert consensus._is_active(conn, '"; DROP TABLE foo; --', "2026-04-30") is False
        assert consensus._is_active(conn, "abc", "2026-04-30") is False
        assert consensus._is_active(conn, "12345", "2026-04-30") is False  # 5 位
    finally:
        conn.close()
        os.unlink(path)


def test_cache_scored_filters_inactive(monkeypatch):
    """cache_scored 集成测试：写入前 _is_active 过滤生效"""
    conn, path = _setup_active_db(monkeypatch)
    try:
        # 000001 活跃，002619 退市
        _make_stock_table(conn, "000001", ["2026-04-29"])
        _make_stock_table(conn, "002619", ["2022-03-31"])
        conn.close()

        df = _make_scored([("000001", 1.0), ("002619", 2.0)])
        n = consensus.cache_scored("2026-04-30", df, top_n=10)
        assert n == 1  # 002619 被守卫过滤

        history = consensus.load_recent_scored("2026-05-01", window=5)
        codes = [c for c, _, _ in history["2026-04-30"]]
        assert codes == ["000001"]
    finally:
        if os.path.exists(path):
            os.unlink(path)
