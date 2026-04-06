"""
每日信号生成器 + 微信推送

收盘后运行，计算今日策略信号并推送操作建议。
用法:
  python main.py signal          # 终端输出
  python main.py signal --push   # 终端输出 + 微信推送
"""

import argparse
import pandas as pd
from datetime import datetime, timedelta
from data.fetcher import fetch_etf_daily
from data.storage import load_daily_data, save_daily_data, get_cached_date_range
from strategy.etf_rotation import ETFRotationStrategy
from portfolio.tracker import PortfolioTracker
from alert.notify import (
    send_message, format_signal_message, format_no_signal_message,
)
from config.settings import ETF_POOL, PUSHPLUS_TOKEN


def run_daily_signal(push: bool = False):
    """生成每日交易信号，可选推送到微信"""
    today = datetime.now().strftime("%Y-%m-%d")
    tracker = PortfolioTracker()

    print(f"\n{'='*50}")
    print(f"每日信号报告 - {today}")
    print(f"{'='*50}")

    # 1. 加载数据（优先本地缓存，增量更新）
    price_data = {}
    for symbol, name in ETF_POOL.items():
        # 先读本地缓存
        df = load_daily_data(symbol)
        cached_end = df["date"].max().strftime("%Y-%m-%d") if not df.empty else None

        # 增量更新：如果缓存不是今天的，补齐到今天
        if cached_end and cached_end < today:
            next_day = (pd.Timestamp(cached_end) + timedelta(days=1)).strftime("%Y-%m-%d")
            try:
                new_df = fetch_etf_daily(symbol, next_day, today)
                if not new_df.empty:
                    save_daily_data(new_df, symbol)
                    df = load_daily_data(symbol)
                    print(f"  [{name}] {symbol}: 增量更新 +{len(new_df)} 条")
            except Exception as e:
                print(f"  [{name}] {symbol}: 增量更新失败 ({e})，使用缓存")
        elif not df.empty:
            pass  # 缓存已是最新
        else:
            # 无缓存，全量下载最近 1 年
            start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
            try:
                df = fetch_etf_daily(symbol, start, today)
                if not df.empty:
                    save_daily_data(df, symbol)
                    print(f"  [{name}] {symbol}: 全量下载 {len(df)} 条")
            except Exception as e:
                print(f"  [{name}] {symbol}: 获取失败 - {e}")

        if not df.empty:
            price_data[symbol] = df
            latest = df["close"].iloc[-1]
            print(f"  [{name}] {symbol}: 最新价 {latest:.3f} ({df['date'].max().strftime('%Y-%m-%d')})")

    if not price_data:
        print("错误: 无法获取任何数据，请检查网络连接")
        if push:
            send_message(
                "ETF轮动 - 数据获取失败",
                f"{today} 数据获取失败，请检查云主机网络。",
                PUSHPLUS_TOKEN,
            )
        return

    # 2. 运行策略生成信号
    strategy = ETFRotationStrategy()
    signals = strategy.generate_signals(price_data)

    if len(signals) == 0:
        print("\n无交易信号")
        if push:
            msg = format_no_signal_message(tracker.get_summary())
            send_message(f"ETF轮动 - {today} 无操作", msg, PUSHPLUS_TOKEN)
        return

    # 3. 取最新信号日期
    latest_date = signals["date"].max()
    today_signals = signals[signals["date"] == latest_date]

    # 终端输出
    print(f"\n{'─'*50}")
    print(f"最新信号 ({pd.Timestamp(latest_date).strftime('%Y-%m-%d')})")

    signals_for_push = []
    for _, sig in today_signals.iterrows():
        action_cn = "买入" if sig["action"] == "buy" else "卖出"
        etf_name = ETF_POOL.get(sig["symbol"], sig["symbol"])
        momentum = sig.get("momentum", 0)
        # 获取最新价
        sym_df = price_data.get(sig["symbol"])
        price = sym_df["close"].iloc[-1] if sym_df is not None and len(sym_df) > 0 else 0

        print(f"  >>> {action_cn} {etf_name}({sig['symbol']}) | 现价 {price:.3f} | 动量: {momentum:+.2%}")
        signals_for_push.append({
            "symbol": sig["symbol"],
            "action": sig["action"],
            "momentum": momentum,
            "price": price,
        })

    # 4. 持仓摘要
    summary = tracker.get_summary()
    print(f"\n{summary}")
    print(f"\n提示: 请在券商 APP 中手动执行上述操作")
    print(f"{'='*50}")

    # 5. 推送到微信
    if push:
        if signals_for_push:
            has_buy = any(s["action"] == "buy" for s in signals_for_push)
            has_sell = any(s["action"] == "sell" for s in signals_for_push)
            tag = ""
            if has_sell:
                tag += "卖出"
            if has_buy:
                tag += "买入"
            title = f"ETF轮动 - {tag}信号 ({pd.Timestamp(latest_date).strftime('%m-%d')})"
            msg = format_signal_message(signals_for_push, ETF_POOL, summary)
        else:
            title = f"ETF轮动 - {today} 无操作"
            msg = format_no_signal_message(summary)

        send_message(title, msg, PUSHPLUS_TOKEN)


def show_portfolio():
    """显示当前持仓"""
    tracker = PortfolioTracker()
    print(tracker.get_summary())
