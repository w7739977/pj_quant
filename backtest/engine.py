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
    price_data: dict[str, pd.DataFrame],
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

    signals = signals.sort_values("date").reset_index(drop=True)

    for _, signal in signals.iterrows():
        date = signal["date"]
        symbol = signal.get("symbol", "")
        action = signal.get("action", "hold")

        # 获取当日价格
        if symbol and symbol in price_data:
            df = price_data[symbol]
            day_price = df[df["date"] <= date]
            if len(day_price) == 0:
                continue
            price = day_price.iloc[-1]["close"]
        else:
            price = 0

        # 执行交易
        if action == "sell" and symbol in holdings:
            shares = holdings[symbol]["shares"]
            if shares > 0:
                costs = calc_trade_cost(price, shares, is_buy=False)
                cash += price * shares - costs["total_cost"]
                trades.append(Trade(
                    date=str(date)[:10], symbol=symbol, action="sell",
                    price=price, shares=shares,
                    commission=costs["commission"],
                    stamp_tax=costs["stamp_tax"],
                    transfer_fee=costs["transfer_fee"],
                    cash_after=round(cash, 2),
                ))
                del holdings[symbol]

        elif action == "buy" and symbol and symbol not in holdings:
            # 用所有可用资金买入（留 100 元余量）
            buy_amount = cash - 100
            if buy_amount > price * 100:  # 至少买 100 股
                shares = int(buy_amount / price / 100) * 100  # 取整到 100 股
                costs = calc_trade_cost(price, shares, is_buy=True)
                total_needed = price * shares + costs["total_cost"]
                if total_needed <= cash:
                    cash -= total_needed
                    holdings[symbol] = {"shares": shares, "avg_cost": price}
                    trades.append(Trade(
                        date=str(date)[:10], symbol=symbol, action="buy",
                        price=price, shares=shares,
                        commission=costs["commission"],
                        stamp_tax=costs["stamp_tax"],
                        transfer_fee=costs["transfer_fee"],
                        cash_after=round(cash, 2),
                    ))

        # 计算当日总净值
        holdings_value = sum(
            price_data[s].loc[price_data[s]["date"] <= date].iloc[-1]["close"]
            * h["shares"]
            for s, h in holdings.items()
            if s in price_data
        )
        nav = cash + holdings_value
        nav_records.append({
            "date": date,
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

    # 日收益率 -> Sharpe（无风险利率按 2%）
    daily_returns = nav_df["nav"].pct_change().dropna()
    sharpe_ratio = 0.0
    if len(daily_returns) > 0 and daily_returns.std() > 0:
        sharpe_ratio = (daily_returns.mean() - 0.02 / 252) / daily_returns.std() * np.sqrt(252)

    return {
        "final_nav": round(final_nav, 2),
        "total_return": total_return,
        "annual_return": annual_return,
        "max_drawdown": round(max_drawdown, 4),
        "sharpe_ratio": round(sharpe_ratio, 2),
        "trade_count": len(nav_df),
        "total_cost": 0,  # 会在外部补充
        "win_rate": 0,    # 会在外部补充
    }
