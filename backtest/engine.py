"""
回测引擎 - 模拟交易执行，计算收益和统计指标
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from config.settings import (
    INITIAL_CAPITAL, COMMISSION_RATE, MIN_COMMISSION,
    STAMP_TAX_RATE, TRANSFER_FEE_RATE,
)


@dataclass
class Trade:
    """交易记录"""
    date: str
    symbol: str
    action: str  # "buy" / "sell"
    price: float
    shares: int
    commission: float
    stamp_tax: float
    transfer_fee: float
    cash_after: float


def calc_trade_cost(price: float, shares: int, is_buy: bool) -> dict:
    """
    计算单笔交易成本

    Returns
    -------
    dict: {commission, stamp_tax, transfer_fee, total_cost}
    """
    amount = price * shares

    # 佣金（买卖都收）
    commission = max(amount * COMMISSION_RATE, MIN_COMMISSION)

    # 印花税（仅卖出）
    stamp_tax = amount * STAMP_TAX_RATE if not is_buy else 0.0

    # 过户费（买卖都收）
    transfer_fee = amount * TRANSFER_FEE_RATE

    return {
        "commission": round(commission, 2),
        "stamp_tax": round(stamp_tax, 2),
        "transfer_fee": round(transfer_fee, 2),
        "total_cost": round(commission + stamp_tax + transfer_fee, 2),
    }


def run_backtest(
    price_data,
    signals: pd.DataFrame,
    initial_capital: float = INITIAL_CAPITAL,
    verbose: bool = True,
) -> dict:
    """
    运行回测

    Parameters
    ----------
    price_data : dict  {symbol: DataFrame with columns [date, open, close, ...]}
    signals : DataFrame  columns: [date, action, symbol]
        action: "buy" or "sell" or "hold"
    initial_capital : float  初始资金

    Returns
    -------
    dict with keys: trades, nav_curve, stats
    """
    cash = initial_capital
    holdings = {}  # {symbol: {"shares": int, "avg_cost": float}}
    trades = []
    nav_records = []

    # M1: 收集所有交易日，按日记录 NAV（而非按 signal）
    all_dates = sorted(set(d for df in price_data.values() for d in df["date"].tolist()))
    signals = signals.sort_values("date").reset_index(drop=True)
    sig_iter = iter(signals.to_dict("records"))
    pending = next(sig_iter, None)

    for d in all_dates:
        # 处理当天所有信号
        while pending and pd.Timestamp(pending["date"]) <= d:
            symbol = pending.get("symbol", "")
            action = pending.get("action", "hold")

            # 获取当日价格
            price = 0
            if symbol and symbol in price_data:
                df = price_data[symbol]
                day_price = df[df["date"] <= d]
                if len(day_price) > 0:
                    price = day_price.iloc[-1]["close"]

            # 执行交易
            if action == "sell" and symbol in holdings:
                shares = holdings[symbol]["shares"]
                if shares > 0 and price > 0:
                    costs = calc_trade_cost(price, shares, is_buy=False)
                    cash += price * shares - costs["total_cost"]
                    trades.append(Trade(
                        date=str(d)[:10], symbol=symbol, action="sell",
                        price=price, shares=shares,
                        commission=costs["commission"],
                        stamp_tax=costs["stamp_tax"],
                        transfer_fee=costs["transfer_fee"],
                        cash_after=round(cash, 2),
                    ))
                    del holdings[symbol]

            elif action == "buy" and symbol and price > 0:
                # M2: 支持加仓（不再限制 symbol not in holdings）
                buy_amount = cash - 100
                if buy_amount > price * 100:
                    shares = int(buy_amount / price / 100) * 100
                    costs = calc_trade_cost(price, shares, is_buy=True)
                    total_needed = price * shares + costs["total_cost"]
                    if total_needed <= cash:
                        cash -= total_needed
                        if symbol in holdings:
                            old = holdings[symbol]
                            total_shares = old["shares"] + shares
                            total_cost = old["avg_cost"] * old["shares"] + price * shares
                            holdings[symbol] = {
                                "shares": total_shares,
                                "avg_cost": total_cost / total_shares,
                            }
                        else:
                            holdings[symbol] = {"shares": shares, "avg_cost": price}
                        trades.append(Trade(
                            date=str(d)[:10], symbol=symbol, action="buy",
                            price=price, shares=shares,
                            commission=costs["commission"],
                            stamp_tax=costs["stamp_tax"],
                            transfer_fee=costs["transfer_fee"],
                            cash_after=round(cash, 2),
                        ))

            pending = next(sig_iter, None)

        # 计算当日总净值
        holdings_value = 0
        for s, h in holdings.items():
            if s in price_data:
                df = price_data[s]
                day_price = df[df["date"] <= d]
                if len(day_price) > 0:
                    holdings_value += day_price.iloc[-1]["close"] * h["shares"]
        nav = cash + holdings_value
        nav_records.append({
            "date": d,
            "nav": round(nav, 2),
            "cash": round(cash, 2),
            "holdings_value": round(holdings_value, 2),
        })

    # 计算统计指标
    nav_df = pd.DataFrame(nav_records)
    stats = _calc_stats(nav_df, initial_capital)

    if verbose and len(trades) > 0:
        print(f"\n{'='*50}")
        print(f"回测统计 ({nav_df['date'].iloc[0]} ~ {nav_df['date'].iloc[-1]})")
        print(f"{'='*50}")
        print(f"初始资金: {initial_capital:,.0f}")
        print(f"最终净值: {stats['final_nav']:,.2f}")
        print(f"总收益率: {stats['total_return']:.2%}")
        print(f"年化收益率: {stats['annual_return']:.2%}")
        print(f"最大回撤: {stats['max_drawdown']:.2%}")
        print(f"交易次数: {stats['trade_count']}")
        print(f"总交易成本: {stats['total_cost']:,.2f}")
        print(f"胜率: {stats['win_rate']:.2%}")
        print(f"{'='*50}")

    return {
        "trades": trades,
        "nav_curve": nav_df,
        "stats": stats,
    }


def _calc_stats(nav_df: pd.DataFrame, initial_capital: float) -> dict:
    """计算回测统计指标"""
    if len(nav_df) < 2:
        return {
            "final_nav": initial_capital,
            "total_return": 0,
            "annual_return": 0,
            "max_drawdown": 0,
            "sharpe_ratio": 0,
            "trade_count": 0,
            "total_cost": 0,
            "win_rate": 0,
        }

    nav = nav_df["nav"].values
    final_nav = nav[-1]
    total_return = (final_nav - initial_capital) / initial_capital

    # 年化收益
    days = (pd.to_datetime(nav_df["date"].iloc[-1]) -
            pd.to_datetime(nav_df["date"].iloc[0])).days
    years = max(days / 365, 1)
    annual_return = (1 + total_return) ** (1 / years) - 1

    # 最大回撤
    peak = np.maximum.accumulate(nav)
    drawdown = (nav - peak) / peak
    max_drawdown = drawdown.min()

    # 日收益率 -> Sharpe
    daily_returns = nav_df["nav"].pct_change().dropna().values
    from analytics.perf import sharpe_ratio
    sr = sharpe_ratio(daily_returns)

    return {
        "final_nav": round(final_nav, 2),
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": round(max_drawdown, 4),
        "sharpe_ratio": round(sr, 2),
        "trade_count": len(nav_df),
        "total_cost": 0,  # 会在外部补充
        "win_rate": 0,    # 会在外部补充
    }
