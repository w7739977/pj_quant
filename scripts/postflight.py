#!/usr/bin/env python3
"""
Postflight 信号归档 — 每日 live 推送后归档当日建议

功能:
  1. 读取当前持仓 + 实时价格
  2. 执行选股逻辑（只读，不推送不操作）
  3. 归档为 logs/signals/YYYY-MM-DD.json
  4. 合理性检查
"""

import sys
import os
import json
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _is_weekend() -> bool:
    """简单判断是否周末"""
    return datetime.now().weekday() >= 5


def _archive_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "signals"
    )


def run():
    # 周末跳过
    if _is_weekend():
        print("非交易日（周末），跳过归档")
        return

    os.makedirs(_archive_dir(), exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    archive_path = os.path.join(_archive_dir(), f"{today}.json")

    # ============ Step 1: 读取当前持仓 ============
    from portfolio.tracker import PortfolioTracker
    from config.settings import INITIAL_CAPITAL, NUM_POSITIONS, MIN_BUY_CAPITAL

    tracker = PortfolioTracker()
    holdings_snapshot = {}
    for code, info in tracker.holdings.items():
        holdings_snapshot[code] = {
            "shares": info["shares"],
            "avg_cost": info["avg_cost"],
            "current_price": None,  # 后面填充
        }

    # 获取实时价格
    price_map = {}
    if holdings_snapshot:
        try:
            from data.fetcher import fetch_realtime_tencent_batch
            codes = list(holdings_snapshot.keys())
            rt_df = fetch_realtime_tencent_batch(codes)
            for _, row in rt_df.iterrows():
                price_map[row["code"]] = float(row.get("price", 0))
        except Exception:
            pass

    for code in holdings_snapshot:
        holdings_snapshot[code]["current_price"] = price_map.get(code)

    # ============ Step 2: 执行选股逻辑（只读） ============
    sell_signals = []
    buy_signals = []

    try:
        from portfolio.allocator import check_holdings, get_stock_picks_live
        from config.settings import STOP_LOSS_PCT, TAKE_PROFIT_PCT, MAX_HOLDING_DAYS

        # 卖出建议
        sell_actions = check_holdings(
            tracker,
            stop_loss_pct=STOP_LOSS_PCT,
            take_profit_pct=TAKE_PROFIT_PCT,
            max_holding_days=MAX_HOLDING_DAYS,
        )
        for a in sell_actions:
            sell_signals.append({
                "code": a["code"],
                "reason": a.get("reason", ""),
                "price": a.get("price"),
                "pnl_pct": a.get("pnl_pct"),
            })
    except Exception as e:
        print(f"  卖出信号获取失败: {e}")

    try:
        # 计算可用资金
        available_cash = tracker.cash
        for a in sell_signals:
            if a.get("price"):
                available_cash += a["price"] * tracker.holdings.get(a["code"], {}).get("shares", 0)

        current_holdings = len(tracker.holdings)
        slots = max(0, NUM_POSITIONS - current_holdings)

        if slots > 0 and available_cash >= MIN_BUY_CAPITAL:
            exclude_codes = list(tracker.holdings.keys())
            picks = get_stock_picks_live(
                stock_capital=available_cash,
                top_n=slots,
                exclude_codes=exclude_codes,
            )
            for p in picks:
                buy_signals.append({
                    "code": p["code"],
                    "shares": p.get("shares"),
                    "price": p.get("price"),
                    "predicted_return": None,  # get_stock_picks_live 不含此字段
                    "reason": p.get("reason", ""),
                })
    except Exception as e:
        print(f"  买入信号获取失败: {e}")

    # ============ Step 3: 计算组合价值 ============
    portfolio_value = tracker.cash
    for code, info in tracker.holdings.items():
        price = price_map.get(code, info["avg_cost"])
        portfolio_value += info["shares"] * price

    # ============ Step 4: 归档 JSON ============
    archive = {
        "date": today,
        "timestamp": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "holdings": holdings_snapshot,
        "sell_signals": sell_signals,
        "buy_signals": buy_signals,
        "portfolio_value": round(portfolio_value, 2),
        "cash": round(tracker.cash, 2),
    }

    with open(archive_path, "w", encoding="utf-8") as f:
        json.dump(archive, f, ensure_ascii=False, indent=2, default=str)

    print(f"  信号已归档: {archive_path}")
    print(f"  持仓: {len(holdings_snapshot)} 只 | 卖出信号: {len(sell_signals)} | 买入信号: {len(buy_signals)}")

    # ============ Step 5: 合理性检查 ============
    _sanity_check(archive, tracker, NUM_POSITIONS)


def _sanity_check(archive: dict, tracker, num_positions: int):
    """合理性检查（不影响归档结果）"""
    warnings = []

    # 检查1: buy_signals 不应和 holdings 重复
    holding_codes = set(archive["holdings"].keys())
    for sig in archive["buy_signals"]:
        if sig["code"] in holding_codes:
            warnings.append(f"买入信号 {sig['code']} 与当前持仓重复")

    # 检查2: 买入总金额不应超过可用现金
    buy_total = sum(
        (sig.get("shares", 0) or 0) * (sig.get("price", 0) or 0)
        for sig in archive["buy_signals"]
    )
    if buy_total > archive["cash"] * 1.1:  # 允许 10% 误差
        warnings.append(f"买入总金额 {buy_total:,.0f} 超过可用现金 {archive['cash']:,.0f}")

    # 检查3: 空信号 + 仓位未满
    if (not archive["sell_signals"] and not archive["buy_signals"]
            and len(archive["holdings"]) < num_positions):
        warnings.append(f"无买卖信号且持仓 {len(archive['holdings'])}/{num_positions} 未满")

    if warnings:
        print("  合理性检查:")
        for w in warnings:
            print(f"    ⚠ {w}")
    else:
        print("  合理性检查: ✓ 通过")


if __name__ == "__main__":
    run()
