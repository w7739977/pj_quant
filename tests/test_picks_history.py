"""portfolio.picks_history 单测"""
import os
import sqlite3
import tempfile

import pandas as pd
import pytest

from portfolio import picks_history


@pytest.fixture
def temp_db(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(picks_history, "DB_PATH", path)
    # 给临时 db 装好 industry_map（is_st 检查）
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE industry_map (code TEXT PRIMARY KEY, name TEXT)")
    conn.execute("INSERT INTO industry_map VALUES ('000001', '平安银行')")
    conn.execute("INSERT INTO industry_map VALUES ('002731', 'ST萃华')")
    conn.commit()
    conn.close()
    yield path
    os.unlink(path)


def _make_stock(path, code, dates_closes):
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE IF NOT EXISTS stock_{code} (date TIMESTAMP, close REAL)")
    for d, p in dates_closes:
        conn.execute(f"INSERT INTO stock_{code} VALUES (?, ?)", (d, p))
    conn.commit()
    conn.close()


# ============ record_picks ============

def test_record_picks_writes_rows(temp_db):
    picks = [
        {"code": "000001", "freq": 5, "avg_score": 1.5},
        {"code": "002731", "freq": 4, "avg_score": 1.3},
    ]
    n = picks_history.record_picks("2026-05-11", picks, pick_top_n=3)
    assert n == 2
    df = picks_history.load_evaluated()
    assert df.empty  # 还没评估
    stats = picks_history.get_stats()
    assert stats["total_picks"] == 2
    assert stats["pending"] == 2


def test_record_picks_marks_st(temp_db):
    picks = [
        {"code": "000001", "freq": 5, "avg_score": 1.5},
        {"code": "002731", "freq": 4, "avg_score": 1.3},  # ST萃华
    ]
    picks_history.record_picks("2026-05-11", picks, pick_top_n=3)
    conn = sqlite3.connect(temp_db)
    rows = dict(conn.execute(
        "SELECT code, is_st FROM picks_history"
    ).fetchall())
    conn.close()
    assert rows == {"000001": 0, "002731": 1}


def test_record_picks_idempotent(temp_db):
    """同 (date, code, top_n) 二次写入应当 REPLACE 不抛"""
    p = [{"code": "000001", "freq": 5, "avg_score": 1.5}]
    picks_history.record_picks("2026-05-11", p, pick_top_n=3)
    picks_history.record_picks("2026-05-11", p, pick_top_n=3)
    assert picks_history.get_stats()["total_picks"] == 1


def test_record_picks_top_n_limit(temp_db):
    """超过 top_n 的不入库"""
    picks = [{"code": f"00000{i}", "freq": 5, "avg_score": 1.0} for i in range(1, 6)]
    n = picks_history.record_picks("2026-05-11", picks, pick_top_n=3)
    assert n == 3


def test_record_picks_empty_safe(temp_db):
    assert picks_history.record_picks("2026-05-11", [], pick_top_n=3) == 0


# ============ _trading_days_after ============

def test_trading_days_after_skips_weekend():
    """周五 + 1 交易日 = 下周一"""
    assert picks_history._trading_days_after("2026-04-30", 1) == "2026-05-06"


def test_trading_days_after_skips_holiday():
    """2026-04-30 周四 + 5 交易日：跳劳动节 5/1-5/5
    5/6 周三 (1)、5/7 周四 (2)、5/8 周五 (3)、5/11 周一 (4)、5/12 周二 (5)"""
    assert picks_history._trading_days_after("2026-04-30", 5) == "2026-05-12"


# ============ evaluate_pending ============

def test_evaluate_pending_no_pending(temp_db):
    """空表 → 空结果不抛"""
    r = picks_history.evaluate_pending(today="2026-05-11")
    assert r == {"evaluated": 0, "skipped": 0, "failed": 0}


def test_evaluate_pending_skips_5d_not_satisfied(temp_db):
    """5d 未满的 picks 应当 skip"""
    picks_history.record_picks(
        "2026-05-11", [{"code": "000001", "freq": 5, "avg_score": 1.0}], pick_top_n=3,
    )
    r = picks_history.evaluate_pending(today="2026-05-12")  # 才过 1 天
    assert r["skipped"] == 1
    assert r["evaluated"] == 0


def test_evaluate_pending_marks_failed_when_no_data(temp_db, monkeypatch):
    """stock 表不存在 → eval_status=failed"""
    picks_history.record_picks(
        "2026-04-30", [{"code": "999999", "freq": 5, "avg_score": 1.0}], pick_top_n=3,
    )
    # mock _compute_bench_5d 避免依赖 get_small_cap_stocks
    monkeypatch.setattr(picks_history, "_compute_bench_5d", lambda *a, **kw: 0.0)
    # 5 交易日后 = 2026-05-12，today 必须 >= 才会触发评估
    r = picks_history.evaluate_pending(today="2026-05-13")
    assert r["failed"] == 1
    conn = sqlite3.connect(temp_db)
    status = conn.execute(
        "SELECT eval_status FROM picks_history WHERE code='999999'"
    ).fetchone()[0]
    conn.close()
    assert status == "failed"


def test_evaluate_pending_computes_alpha(temp_db, monkeypatch):
    """有完整数据 → ret/alpha 正确回填。
    eval_date = 2026-04-30 + 5 交易日 = 2026-05-12 (劳动节 5/1-5/5 全休)"""
    _make_stock(temp_db, "000001", [
        ("2026-04-30", 10.0),
        ("2026-05-12", 11.0),  # eval_date (5d 工作日后)
    ])
    picks_history.record_picks(
        "2026-04-30", [{"code": "000001", "freq": 5, "avg_score": 1.0}], pick_top_n=3,
    )
    monkeypatch.setattr(picks_history, "_compute_bench_5d",
                        lambda *a, **kw: 0.05)  # mock 基准 +5%
    r = picks_history.evaluate_pending(today="2026-05-13")
    assert r["evaluated"] == 1
    df = picks_history.load_evaluated()
    assert len(df) == 1
    assert df.iloc[0]["ret_5d"] == pytest.approx(0.10, abs=1e-4)  # 10 → 11
    assert df.iloc[0]["alpha"] == pytest.approx(0.05, abs=1e-4)  # 0.10 - 0.05


def test_evaluate_pending_idempotent(temp_db, monkeypatch):
    """已评估的 picks 不重复评估"""
    _make_stock(temp_db, "000001", [
        ("2026-04-30", 10.0), ("2026-05-12", 11.0),
    ])
    picks_history.record_picks(
        "2026-04-30", [{"code": "000001", "freq": 5, "avg_score": 1.0}], pick_top_n=3,
    )
    monkeypatch.setattr(picks_history, "_compute_bench_5d", lambda *a, **kw: 0.0)
    r1 = picks_history.evaluate_pending(today="2026-05-13")
    r2 = picks_history.evaluate_pending(today="2026-05-13")
    assert r1["evaluated"] == 1
    assert r2["evaluated"] == 0  # 第二次没东西可评估


# ============ get_stats ============

def test_get_stats_empty(temp_db):
    s = picks_history.get_stats()
    assert s["total_picks"] == 0
    assert s["weeks_to_significance"] == 80


def test_get_stats_progress(temp_db):
    for d in ["2026-05-04", "2026-05-11", "2026-05-18"]:
        picks_history.record_picks(
            d, [{"code": "000001", "freq": 5, "avg_score": 1.0}], pick_top_n=3,
        )
    s = picks_history.get_stats()
    assert s["n_weeks"] == 3
    assert s["total_picks"] == 3
    assert s["weeks_to_significance"] == 77


# ============ load_evaluated ============

def test_load_evaluated_filters(temp_db, monkeypatch):
    _make_stock(temp_db, "000001", [("2026-04-30", 10.0), ("2026-05-12", 11.0)])
    _make_stock(temp_db, "002731", [("2026-04-30", 5.0), ("2026-05-12", 5.5)])
    picks_history.record_picks("2026-04-30", [
        {"code": "000001", "freq": 5, "avg_score": 1.0},
        {"code": "002731", "freq": 5, "avg_score": 0.9},
    ], pick_top_n=3)
    monkeypatch.setattr(picks_history, "_compute_bench_5d", lambda *a, **kw: 0.0)
    picks_history.evaluate_pending(today="2026-05-13")

    all_df = picks_history.load_evaluated()
    no_st = picks_history.load_evaluated(exclude_st=True)
    assert len(all_df) == 2
    assert len(no_st) == 1
    assert no_st.iloc[0]["code"] == "000001"
