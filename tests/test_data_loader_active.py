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


FIXED_TODAY = "2026-04-30"  # 周四，无相邻节假日干扰，便于测试稳定


def test_filter_active_keeps_recent(stock_db):
    """昨日活跃股 (1 个交易日 gap) 保留"""
    from factors.data_loader import _filter_active
    conn = sqlite3.connect(stock_db)
    try:
        _make_stock(conn, "000001", "2026-04-29")  # gap=1 交易日
    finally:
        conn.close()
    df = pd.DataFrame([{"code": "000001", "market_cap": 1e9}])
    out = _filter_active(df, today=FIXED_TODAY)
    assert list(out["code"]) == ["000001"]


def test_filter_active_drops_stale(stock_db):
    """last_bar 距今数年 → 剔除"""
    from factors.data_loader import _filter_active
    conn = sqlite3.connect(stock_db)
    try:
        _make_stock(conn, "002619", "2022-03-31")
    finally:
        conn.close()
    df = pd.DataFrame([{"code": "002619", "market_cap": 1e9}])
    out = _filter_active(df, today=FIXED_TODAY)
    assert out.empty


def test_filter_active_drops_missing_table(stock_db):
    """stock_{code} 表不存在 → 剔除（不抛）"""
    from factors.data_loader import _filter_active
    df = pd.DataFrame([{"code": "999999", "market_cap": 1e9}])
    out = _filter_active(df, today=FIXED_TODAY)
    assert out.empty


def test_filter_active_drops_invalid_code(stock_db):
    """code 非 6 位数字 → 通过 _safe_table_name 拦下，剔除（不抛）"""
    from factors.data_loader import _filter_active
    df = pd.DataFrame([{"code": "abc", "market_cap": 1e9}])
    out = _filter_active(df, today=FIXED_TODAY)
    assert out.empty


def test_filter_active_empty_safe(stock_db):
    """空 df 直接返回"""
    from factors.data_loader import _filter_active
    out = _filter_active(pd.DataFrame(), today=FIXED_TODAY)
    assert out.empty


def test_filter_active_mixed(stock_db):
    """混合：1 活跃 + 2 陈旧 + 1 缺表，只留活跃"""
    from factors.data_loader import _filter_active
    conn = sqlite3.connect(stock_db)
    try:
        _make_stock(conn, "000001", "2026-04-28")  # 活跃 (2 个交易日 gap)
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
    out = _filter_active(df, today=FIXED_TODAY)
    assert list(out["code"]) == ["000001"]


def test_filter_active_custom_max_gap(stock_db):
    """自定义 max_gap_days 生效（交易日数）"""
    from factors.data_loader import _filter_active
    conn = sqlite3.connect(stock_db)
    try:
        _make_stock(conn, "000001", "2026-04-27")  # 周一 → 4-30 周四 gap=3
    finally:
        conn.close()
    df = pd.DataFrame([{"code": "000001", "market_cap": 1e9}])
    assert _filter_active(df, max_gap_days=2, today=FIXED_TODAY).empty
    assert list(_filter_active(df, max_gap_days=5, today=FIXED_TODAY)["code"]) == ["000001"]


def test_filter_active_skips_holiday(stock_db):
    """节后查节前 last_bar：自然日跨假期但交易日 gap 小，保留"""
    from factors.data_loader import _filter_active
    # 2026-04-30(周四) → 2026-05-08(周五)：劳动节连休 5-1~5-5，工作日 gap=3
    conn = sqlite3.connect(stock_db)
    try:
        _make_stock(conn, "000001", "2026-04-30")
    finally:
        conn.close()
    df = pd.DataFrame([{"code": "000001", "market_cap": 1e9}])
    out = _filter_active(df, today="2026-05-08")
    assert list(out["code"]) == ["000001"]
