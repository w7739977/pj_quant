"""
模拟撮合器

A股交易规则:
  - 市价买入: 以卖一价(ask1)成交 + 滑点
  - 市价卖出: 以买一价(bid1)成交 - 滑点
  - 涨停板(ask1<=0): 无法买入
  - 跌停板(bid1<=0): 无法卖出
  - T+1: 当日买入不可当日卖出
  - 100股整手
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import chinese_calendar
from portfolio.trade_utils import is_tradeable, calc_shares, estimate_buy_cost, estimate_sell_cost

logger = logging.getLogger(__name__)

# 默认滑点（元）
DEFAULT_SLIPPAGE = 0.01


def _calc_days(buy_date: str) -> int:
    """计算持有天数（日历日）"""
    if not buy_date:
        return 0
    try:
        buy_dt = datetime.strptime(buy_date, "%Y-%m-%d")
        return (datetime.now() - buy_dt).days
    except ValueError:
        return 0


def _calc_trade_days(buy_date: str) -> int:
    """计算持有交易日数（排除周末和法定节假日）"""
    if not buy_date:
        return 0
    try:
        buy_dt = datetime.strptime(buy_date, "%Y-%m-%d").date()
        today = datetime.now().date()
        if buy_dt >= today:
            return 0
        workdays = chinese_calendar.get_workdays(buy_dt, today)
        # get_workdays returns a list of dates; exclude buy_date itself
        if isinstance(workdays, list):
            return max(0, len(workdays) - 1)
        return int(workdays) - 1 if workdays > 0 else 0
    except Exception:
        return _calc_days(buy_date)


def _check_limit(code: str, price: float, prev_close: float) -> tuple:
    """
    判断涨跌停状态

    Returns
    -------
    (is_limit_up, is_limit_down)
    """
    if prev_close <= 0 or price <= 0:
        return False, False

    if code.startswith("688") or code.startswith("300"):  # 科创板 + 创业板 20%
        limit_pct = 0.20
    elif code.startswith(("8", "4")):                     # 北交所 30%
        limit_pct = 0.30
    else:                                                  # 沪深主板 10%
        limit_pct = 0.10

    is_limit_up = price >= prev_close * (1 + limit_pct - 0.001)
    is_limit_down = price <= prev_close * (1 - limit_pct + 0.001)
    return is_limit_up, is_limit_down


class Order:
    """订单"""
    __slots__ = ("order_id", "symbol", "side", "order_type", "shares", "price",
                 "created_at", "reason", "status", "filled_price", "filled_shares",
                 "fee")

    _counter = 0

    def __init__(self, symbol: str, side: str, shares: int,
                 price: float = 0.0, order_type: str = "market",
                 reason: str = ""):
        Order._counter += 1
        self.order_id = Order._counter
        self.symbol = symbol
        self.side = side          # "buy" / "sell"
        self.order_type = order_type  # "market" / "limit"
        self.shares = shares
        self.price = price        # 限价单价格
        self.created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.reason = reason
        self.status = "pending"   # pending / filled / cancelled / rejected
        self.filled_price = 0.0
        self.filled_shares = 0
        self.fee = 0.0


class Matcher:
    """模拟撮合器"""

    def __init__(self, slippage: float = DEFAULT_SLIPPAGE):
        self.slippage = slippage

    def match(self, order: Order, quote: dict) -> Order:
        """
        撮合单笔订单

        Parameters
        ----------
        order : Order
        quote : dict  实时行情 {price, bid1, ask1, high, low, ...}

        Returns
        -------
        Order (status updated)
        """
        if order.status != "pending":
            return order

        current_price = quote.get("price", 0)
        if current_price <= 0:
            order.status = "rejected"
            order.reason += " [无行情]"
            return order

        if order.side == "buy":
            return self._match_buy(order, quote)
        else:
            return self._match_sell(order, quote)

    def _match_buy(self, order: Order, quote: dict) -> Order:
        """买入撮合"""
        price = quote.get("price", 0)
        prev_close = quote.get("prev_close", 0)

        # 涨停检查
        is_limit_up, _ = _check_limit(order.symbol, price, prev_close)
        if is_limit_up:
            order.status = "rejected"
            order.reason += " [涨停]"
            return order

        ask1 = quote.get("ask1", 0)
        if ask1 <= 0:
            ask1 = price

        fill_price = ask1 + self.slippage

        # 限价单检查
        if order.order_type == "limit" and order.price > 0:
            if fill_price > order.price:
                return order  # 不满足条件，继续挂单

        order.filled_price = round(fill_price, 3)
        order.filled_shares = order.shares
        order.fee = estimate_buy_cost(fill_price * order.shares)
        order.status = "filled"
        return order

    def _match_sell(self, order: Order, quote: dict) -> Order:
        """卖出撮合"""
        price = quote.get("price", 0)
        prev_close = quote.get("prev_close", 0)

        # 跌停检查
        _, is_limit_down = _check_limit(order.symbol, price, prev_close)
        if is_limit_down:
            order.status = "rejected"
            order.reason += " [跌停]"
            return order

        bid1 = quote.get("bid1", 0)
        if bid1 <= 0:
            bid1 = price

        fill_price = bid1 - self.slippage

        # 限价单检查
        if order.order_type == "limit" and order.price > 0:
            if fill_price < order.price:
                return order  # 不满足条件，继续挂单

        order.filled_price = round(fill_price, 3)
        order.filled_shares = order.shares
        order.fee = estimate_sell_cost(fill_price * order.shares)
        order.status = "filled"
        return order

    def check_stop_loss(self, symbol: str, shares: int, avg_cost: float,
                        buy_date: str, quote: dict,
                        stop_loss_pct: float = -0.08,
                        take_profit_pct: float = 0.15,
                        max_holding_days: int = 20) -> Optional[Order]:
        """
        实时止损/止盈/超时检查

        Returns
        -------
        Order if triggered, else None
        """
        current_price = quote.get("price", 0)
        if current_price <= 0 or avg_cost <= 0:
            return None

        pnl_pct = current_price / avg_cost - 1.0
        pnl_amount = (current_price - avg_cost) * shares
        reason = ""

        if pnl_pct <= stop_loss_pct:
            reason = f"止损({pnl_pct:+.1%}, 持有{_calc_trade_days(buy_date)}交易日, {pnl_amount:+,.0f}元)"
        elif pnl_pct >= take_profit_pct:
            reason = f"止盈({pnl_pct:+.1%}, 持有{_calc_trade_days(buy_date)}交易日, {pnl_amount:+,.0f}元)"
        elif buy_date:
            trade_days = _calc_trade_days(buy_date)
            if trade_days >= max_holding_days and abs(pnl_pct) < 0.03:
                reason = f"超时调仓(持有{trade_days}交易日, 收益{pnl_pct:+.1%}, {pnl_amount:+,.0f}元)"

        if not reason:
            return None

        order = Order(symbol, "sell", shares, reason=reason)
        return order

    def can_sell_today(self, buy_date: str) -> bool:
        """T+1 检查: 当日买入不可当日卖出"""
        if not buy_date:
            return True
        today = datetime.now().strftime("%Y-%m-%d")
        return buy_date < today


def fetch_quotes_batch(symbols: list) -> dict:
    """
    批量获取行情（腾讯接口）

    Returns
    -------
    dict: {symbol: quote_dict}
    """
    from data.fetcher import fetch_realtime_tencent_batch
    try:
        rt_df = fetch_realtime_tencent_batch(symbols)
        result = {}
        for _, row in rt_df.iterrows():
            code = row["code"]
            price = float(row.get("price", 0))
            prev_close = float(row.get("prev_close", 0)) if "prev_close" in row else 0
            is_limit_up, is_limit_down = _check_limit(code, price, prev_close)
            result[code] = {
                "price": price,
                "bid1": price,
                "ask1": price,
                "high": float(row.get("high", 0)) if "high" in row else 0,
                "low": float(row.get("low", 0)) if "low" in row else 0,
                "prev_close": prev_close,
                "name": row.get("name", ""),
                "volume": float(row.get("volume", 0)),
                "change_pct": float(row.get("change_pct", 0)),
                "is_limit_up": is_limit_up,
                "is_limit_down": is_limit_down,
            }
        return result
    except Exception as e:
        logger.warning(f"批量行情获取失败: {e}")
        return {}
