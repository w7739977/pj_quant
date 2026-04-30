"""
模拟交易记录 + 每日快照

SQLite 独立数据库 sim_trading.db:
  - sim_orders:    订单记录
  - sim_trades:    成交记录
  - sim_snapshots: 每日收盘快照
"""

from __future__ import annotations

import sqlite3
import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

SIM_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "data", "sim_trading.db")


_db_initialized = False


def _get_conn() -> sqlite3.Connection:
    global _db_initialized
    os.makedirs(os.path.dirname(SIM_DB_PATH), exist_ok=True)
    conn = sqlite3.connect(SIM_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    if _db_initialized:
        _ensure_reason_data_column(conn)
    return conn


def _ensure_reason_data_column(conn):
    """幂等迁移：为老库添加 reason_data 列"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sim_trades)").fetchall()}
    if "reason_data" not in cols:
        conn.execute("ALTER TABLE sim_trades ADD COLUMN reason_data TEXT DEFAULT '{}'")
        conn.commit()


def init_db():
    """初始化表结构"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sim_orders (
            order_id    INTEGER PRIMARY KEY,
            date        TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            side        TEXT NOT NULL,
            shares      INTEGER NOT NULL,
            price       REAL DEFAULT 0,
            reason      TEXT DEFAULT '',
            status      TEXT DEFAULT 'pending',
            filled_price REAL DEFAULT 0,
            filled_shares INTEGER DEFAULT 0,
            fee         REAL DEFAULT 0,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS sim_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            date        TEXT NOT NULL,
            symbol      TEXT NOT NULL,
            name        TEXT DEFAULT '',
            side        TEXT NOT NULL,
            shares      INTEGER NOT NULL,
            price       REAL NOT NULL,
            amount      REAL NOT NULL,
            fee         REAL DEFAULT 0,
            profit      REAL DEFAULT 0,
            reason      TEXT DEFAULT '',
            order_id    INTEGER
        );

        CREATE TABLE IF NOT EXISTS sim_snapshots (
            date            TEXT PRIMARY KEY,
            cash            REAL NOT NULL,
            market_value    REAL NOT NULL,
            total_value     REAL NOT NULL,
            daily_return    REAL DEFAULT 0,
            total_return    REAL DEFAULT 0,
            positions_json  TEXT DEFAULT '{}',
            trades_json     TEXT DEFAULT '[]'
        );
    """)
    # 幂等迁移: 确保老库有 reason_data 列
    _ensure_reason_data_column(conn)
    global _db_initialized
    _db_initialized = True
    conn.close()


# ============ 订单记录 ============

def save_order(order) -> int:
    """保存订单"""
    conn = _get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO sim_orders
        (order_id, date, symbol, side, shares, price, reason,
         status, filled_price, filled_shares, fee, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order.order_id,
        datetime.now().strftime("%Y-%m-%d"),
        order.symbol,
        order.side,
        order.shares,
        order.price,
        order.reason,
        order.status,
        order.filled_price,
        order.filled_shares,
        order.fee,
        order.created_at,
    ))
    conn.commit()
    conn.close()
    return order.order_id


def update_order_status(order):
    """更新订单状态"""
    conn = _get_conn()
    conn.execute("""
        UPDATE sim_orders
        SET status=?, filled_price=?, filled_shares=?, fee=?
        WHERE order_id=?
    """, (order.status, order.filled_price, order.filled_shares,
          order.fee, order.order_id))
    conn.commit()
    conn.close()


# ============ 成交记录 ============

def save_trade(symbol: str, name: str, side: str, shares: int,
               price: float, amount: float, fee: float,
               profit: float = 0.0, reason: str = "",
               order_id: int = 0, reason_data=None) -> int:
    """保存成交记录"""
    # reason_data: 接受 dict 或 str，统一序列化为 JSON 字符串
    if isinstance(reason_data, dict):
        reason_data = json.dumps(reason_data, ensure_ascii=False)
    elif not reason_data:
        reason_data = "{}"
    conn = _get_conn()
    cur = conn.execute("""
        INSERT INTO sim_trades
        (date, symbol, name, side, shares, price, amount, fee, profit, reason, order_id, reason_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        datetime.now().strftime("%Y-%m-%d"),
        symbol, name, side, shares, price, amount, fee,
        profit, reason, order_id, reason_data,
    ))
    conn.commit()
    trade_id = cur.lastrowid
    conn.close()
    return trade_id


def get_today_trades() -> list[dict]:
    """获取今日成交"""
    conn = _get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT * FROM sim_trades WHERE date=? ORDER BY id", (today,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_trades(start_date: str = None, end_date: str = None,
               symbol: str = None) -> list[dict]:
    """查询成交记录"""
    conn = _get_conn()
    sql = "SELECT * FROM sim_trades WHERE 1=1"
    params = []
    if start_date:
        sql += " AND date >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND date <= ?"
        params.append(end_date)
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    sql += " ORDER BY date, id"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ============ 每日快照 ============

def save_snapshot(cash: float, market_value: float, total_value: float,
                  daily_return: float, total_return: float,
                  positions: dict, trades: list):
    """保存每日收盘快照"""
    conn = _get_conn()
    today = datetime.now().strftime("%Y-%m-%d")
    conn.execute("""
        INSERT OR REPLACE INTO sim_snapshots
        (date, cash, market_value, total_value, daily_return, total_return,
         positions_json, trades_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        today, cash, market_value, total_value,
        daily_return, total_return,
        json.dumps(positions, ensure_ascii=False),
        json.dumps(trades, ensure_ascii=False),
    ))
    conn.commit()
    conn.close()


def get_latest_snapshot() -> dict | None:
    """获取最近一次快照"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM sim_snapshots ORDER BY date DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    result["positions"] = json.loads(result.get("positions_json", "{}"))
    result["trades"] = json.loads(result.get("trades_json", "[]"))
    return result


def get_snapshots(limit: int = 30) -> list[dict]:
    """获取最近N天的快照"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM sim_snapshots ORDER BY date DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    results = []
    for row in rows:
        r = dict(row)
        r["positions"] = json.loads(r.get("positions_json", "{}"))
        r["trades"] = json.loads(r.get("trades_json", "[]"))
        results.append(r)
    return results


# ============ 模拟盘持仓状态 ============

SIM_PORTFOLIO_PATH = os.path.join(os.path.dirname(SIM_DB_PATH),
                                  "sim_portfolio.json")


def save_sim_portfolio(state: dict):
    """保存模拟盘持仓"""
    with open(SIM_PORTFOLIO_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def load_sim_portfolio() -> dict:
    """加载模拟盘持仓"""
    from config.settings import SIM_INITIAL_CAPITAL
    if not os.path.exists(SIM_PORTFOLIO_PATH):
        return {"cash": SIM_INITIAL_CAPITAL, "holdings": {}}
    try:
        with open(SIM_PORTFOLIO_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"cash": SIM_INITIAL_CAPITAL, "holdings": {}}


# ============ 初始化 ============

init_db()
