"""
模拟盘交易日志 — SQLite 持久化

三张表:
  - sim_orders: 订单记录
  - sim_trades: 成交记录
  - sim_snapshots: 每日快照
"""

import json
import sqlite3
import os
import logging
from datetime import datetime
from typing import Optional, List, Dict

logger = logging.getLogger(__name__)

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "sim_trading.db",
)
_PORTFOLIO_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "sim_portfolio.json",
)


def _get_conn() -> sqlite3.Connection:
    """获取 SQLite 连接（自动建表）"""
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sim_orders (
            order_id INTEGER PRIMARY KEY,
            symbol TEXT,
            side TEXT,
            order_type TEXT,
            shares INTEGER,
            price REAL,
            reason TEXT,
            status TEXT,
            filled_price REAL,
            filled_shares INTEGER,
            fee REAL,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sim_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            symbol TEXT,
            name TEXT,
            side TEXT,
            shares INTEGER,
            price REAL,
            amount REAL,
            fee REAL,
            profit REAL DEFAULT 0,
            reason TEXT DEFAULT '',
            order_id INTEGER DEFAULT 0,
            reason_data TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS sim_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT,
            cash REAL,
            market_value REAL,
            total_value REAL,
            daily_return REAL,
            total_return REAL,
            positions TEXT DEFAULT '{}',
            trades TEXT DEFAULT '[]'
        );
    """)
    return conn


def load_sim_portfolio() -> dict:
    """加载模拟持仓"""
    if not os.path.exists(_PORTFOLIO_PATH):
        return {"cash": 20000.0, "holdings": {}}
    try:
        with open(_PORTFOLIO_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"cash": 20000.0, "holdings": {}}


def save_sim_portfolio(portfolio: dict) -> None:
    """保存模拟持仓"""
    os.makedirs(os.path.dirname(_PORTFOLIO_PATH), exist_ok=True)
    with open(_PORTFOLIO_PATH, "w") as f:
        json.dump(portfolio, f, ensure_ascii=False, indent=2)


def save_order(order) -> None:
    """写入订单"""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO sim_orders
               (order_id, symbol, side, order_type, shares, price,
                reason, status, filled_price, filled_shares, fee, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order.order_id, order.symbol, order.side, order.order_type,
             order.shares, order.price, order.reason, order.status,
             order.filled_price, order.filled_shares, order.fee,
             order.created_at),
        )
        conn.commit()
    finally:
        conn.close()


def update_order_status(order) -> None:
    """更新订单状态"""
    conn = _get_conn()
    try:
        conn.execute(
            """UPDATE sim_orders
               SET status=?, filled_price=?, filled_shares=?, fee=?, reason=?
               WHERE order_id=?""",
            (order.status, order.filled_price, order.filled_shares,
             order.fee, order.reason, order.order_id),
        )
        conn.commit()
    finally:
        conn.close()


def save_trade(symbol: str, name: str, side: str, shares: int,
               price: float, amount: float, fee: float, *,
               profit: float = 0.0, reason: str = "",
               order_id: int = 0, reason_data: str = "") -> None:
    """写入成交记录"""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO sim_trades
               (date, symbol, name, side, shares, price, amount, fee,
                profit, reason, order_id, reason_data)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
             symbol, name, side, shares, price, amount, fee,
             profit, reason, order_id, reason_data),
        )
        conn.commit()
    finally:
        conn.close()


def save_snapshot(*, cash, market_value, total_value,
                  daily_return, total_return, positions, trades) -> None:
    """保存每日快照"""
    conn = _get_conn()
    try:
        conn.execute(
            """INSERT INTO sim_snapshots
               (date, cash, market_value, total_value, daily_return,
                total_return, positions, trades)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (datetime.now().strftime("%Y-%m-%d"),
             cash, market_value, total_value, daily_return, total_return,
             json.dumps(positions, ensure_ascii=False),
             json.dumps(trades, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_snapshot() -> Optional[dict]:
    """获取最近一次快照"""
    conn = _get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM sim_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        # 解析 JSON 字段
        for key in ("positions", "trades"):
            if isinstance(d.get(key), str):
                try:
                    d[key] = json.loads(d[key])
                except (json.JSONDecodeError, TypeError):
                    d[key] = {} if key == "positions" else []
        return d
    finally:
        conn.close()


def get_today_trades() -> List[dict]:
    """获取今日成交"""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM sim_trades WHERE date LIKE ? ORDER BY id",
            (f"{today}%",),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_trades(start_date: str = None, end_date: str = None) -> List[dict]:
    """获取成交记录（按日期范围）"""
    conn = _get_conn()
    try:
        sql = "SELECT * FROM sim_trades WHERE 1=1"
        params = []
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(f"{end_date} 23:59:59")
        sql += " ORDER BY id"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_snapshots(limit: int = 30) -> List[dict]:
    """获取快照（按日期降序）"""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM sim_snapshots ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            for key in ("positions", "trades"):
                if isinstance(d.get(key), str):
                    try:
                        d[key] = json.loads(d[key])
                    except (json.JSONDecodeError, TypeError):
                        d[key] = {} if key == "positions" else []
            results.append(d)
        return results
    finally:
        conn.close()
