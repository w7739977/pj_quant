"""5 天频次共识选股（D 方案）单测"""
import os
import sqlite3
import tempfile

import pandas as pd
import pytest

from portfolio import consensus


@pytest.fixture
def temp_db(monkeypatch):
    """每个测试用独立临时 SQLite"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(consensus, "DB_PATH", path)
    yield path
    os.unlink(path)


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
