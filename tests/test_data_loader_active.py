"""factors.data_loader._filter_active 单测

pool 层活跃度过滤是 portfolio.consensus 新鲜度守卫的冗余防线，覆盖：
- 真实活跃股保留 / 陈旧 bar 剔除
- 表不存在 / code 非法（非 6 位数字）
- 空输入安全
"""
import os
import sqlite3
import tempfile

import pandas as pd
import pytest


def _make_stock(conn: sqlite3.Connection, code: str, last_bar: str) -> None:
    conn.execute(f"CREATE TABLE stock_{code} (date TIMESTAMP, close REAL)")
    conn.execute(f"INSERT INTO stock_{code} VALUES (?, 10.0)", (last_bar,))
    conn.commit()


@pytest.fixture
def stock_db(monkeypatch):
    """临时 db + monkeypatch config.settings.DB_PATH"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import config.settings
    monkeypatch.setattr(config.settings, "DB_PATH", path)
    yield path
    os.unlink(path)


def test_filter_active_keeps_recent(stock_db):
    """last_bar 在守卫阈值内 → 保留"""
    from factors.data_loader import _filter_active
    today = pd.Timestamp.now().normalize()
    recent_bar = (today - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(stock_db)
    try:
        _make_stock(conn, "000001", recent_bar)
    finally:
        conn.close()
    df = pd.DataFrame([{"code": "000001", "market_cap": 1e9}])
    out = _filter_active(df)
    assert list(out["code"]) == ["000001"]


def test_filter_active_drops_stale(stock_db):
    """last_bar 距今超阈值 → 剔除"""
    from factors.data_loader import _filter_active
    conn = sqlite3.connect(stock_db)
    try:
        _make_stock(conn, "002619", "2022-03-31")
    finally:
        conn.close()
    df = pd.DataFrame([{"code": "002619", "market_cap": 1e9}])
    out = _filter_active(df)
    assert out.empty


def test_filter_active_drops_missing_table(stock_db):
    """stock_{code} 表不存在 → 剔除（不抛）"""
    from factors.data_loader import _filter_active
    df = pd.DataFrame([{"code": "999999", "market_cap": 1e9}])
    out = _filter_active(df)
    assert out.empty


def test_filter_active_drops_invalid_code(stock_db):
    """code 非 6 位数字 → 通过 _safe_table_name 拦下，剔除（不抛）"""
    from factors.data_loader import _filter_active
    df = pd.DataFrame([{"code": "abc", "market_cap": 1e9}])
    out = _filter_active(df)
    assert out.empty


def test_filter_active_empty_safe(stock_db):
    """空 df 直接返回"""
    from factors.data_loader import _filter_active
    out = _filter_active(pd.DataFrame())
    assert out.empty


def test_filter_active_mixed(stock_db):
    """混合：1 活跃 + 2 陈旧 + 1 缺表，只留活跃"""
    from factors.data_loader import _filter_active
    today = pd.Timestamp.now().normalize()
    recent = (today - pd.Timedelta(days=2)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(stock_db)
    try:
        _make_stock(conn, "000001", recent)        # 活跃
        _make_stock(conn, "002619", "2022-03-31")  # 陈旧
        _make_stock(conn, "600677", "2020-04-29")  # 陈旧
        # 600999 不建表 → 缺表
    finally:
        conn.close()
    df = pd.DataFrame([
        {"code": "000001", "market_cap": 1e9},
        {"code": "002619", "market_cap": 9e8},
        {"code": "600677", "market_cap": 8e8},
        {"code": "600999", "market_cap": 7e8},
    ])
    out = _filter_active(df)
    assert list(out["code"]) == ["000001"]


def test_filter_active_custom_max_gap(stock_db):
    """自定义 max_gap_days 生效"""
    from factors.data_loader import _filter_active
    today = pd.Timestamp.now().normalize()
    bar_3d_ago = (today - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(stock_db)
    try:
        _make_stock(conn, "000001", bar_3d_ago)
    finally:
        conn.close()
    df = pd.DataFrame([{"code": "000001", "market_cap": 1e9}])
    # max_gap_days=2: 3 天前的 bar 超阈值，剔除
    assert _filter_active(df, max_gap_days=2).empty
    # max_gap_days=5: 3 天前的 bar 在阈值内，保留
    assert list(_filter_active(df, max_gap_days=5)["code"]) == ["000001"]
