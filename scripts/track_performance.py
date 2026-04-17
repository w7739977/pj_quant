#!/usr/bin/env python3
"""
信号绩效追踪 — 读取历史信号归档，对比实际表现

用法: python3 scripts/track_performance.py [--push]
"""

import sys
import os
import json
import glob
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _signal_dir() -> str:
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "logs", "signals"
    )


def load_signals() -> list:
    """读取所有历史信号文件"""
    sig_dir = _signal_dir()
    if not os.path.exists(sig_dir):
        return []

    files = sorted(glob.glob(os.path.join(sig_dir, "*.json")))
    signals = []
    for fp in files:
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["_file"] = os.path.basename(fp)
                signals.append(data)
        except Exception:
            continue
    return signals


def get_future_returns(code: str, signal_date: str, periods: list = [5, 10, 20]) -> dict:
    """
    获取某股票从 signal_date 起 T+N 的实际收益率

    Returns: {5: 0.023, 10: 0.051, 20: 0.078} 或 {} (数据不足)
    """
    import pandas as pd
    from data.storage import load_stock_daily

    df = load_stock_daily(code)
    if df.empty:
        return {}

    # 找到 signal_date 或之后的第一天
    df = df.sort_values("date").reset_index(drop=True)
    signal_dt = pd.to_datetime(signal_date)
    mask = df["date"] >= signal_dt
    if not mask.any():
        return {}

    start_idx = df[mask].index[0]
    if start_idx >= len(df) - 1:
        return {}

    # 用 signal_date 后一天的价格作为基准（买入价）
    buy_price = float(df.iloc[start_idx]["close"])
    if buy_price <= 0:
        return {}

    results = {}
    for n in periods:
        target_idx = start_idx + n
        if target_idx < len(df):
            sell_price = float(df.iloc[target_idx]["close"])
            if sell_price > 0:
                results[n] = (sell_price / buy_price - 1.0)
    return results


def run(push: bool = False) -> str:
    import pandas as pd
    import numpy as np

    signals = load_signals()

    if not signals:
        msg = "无历史信号数据 (logs/signals/ 为空或不存在)"
        print(msg)
        return msg

    # 日期范围
    dates = [s["date"] for s in signals if s.get("date")]
    if not dates:
        msg = "信号文件中无有效日期"
        print(msg)
        return msg

    date_range = f"{min(dates)} ~ {max(dates)}"

    # 收集所有买入和卖出信号
    all_buys = []
    all_sells = []
    for sig in signals:
        d = sig["date"]
        for b in sig.get("buy_signals", []):
            b["signal_date"] = d
            all_buys.append(b)
        for s in sig.get("sell_signals", []):
            s["signal_date"] = d
            all_sells.append(s)

    report_lines = []
    report_lines.append("═══════ 信号绩效报告 ═══════")
    report_lines.append(f"统计周期: {date_range}")
    report_lines.append(f"总信号数: {len(all_buys)} 买入 / {len(all_sells)} 卖出")
    report_lines.append("")

    # ============ 买入信号表现 ============
    buy_results = []
    for b in all_buys:
        code = b.get("code")
        if not code:
            continue
        future = get_future_returns(code, b["signal_date"])
        if future:
            buy_results.append({
                "date": b["signal_date"],
                "code": code,
                "predicted": b.get("predicted_return"),
                **future,
            })

    if buy_results:
        # 表格
        report_lines.append("买入信号表现（已有 T+20 数据的）:")
        report_lines.append("┌──────────┬────────┬─────────┬─────────┬─────────┬──────────┐")
        report_lines.append("│ 日期     │ 代码   │ 预测收益 │ T+5实际 │ T+10实际│ T+20实际 │")
        report_lines.append("├──────────┼────────┼─────────┼─────────┼─────────┼──────────┤")

        for r in buy_results:
            date_short = r["date"][5:]  # MM-DD
            pred = f"{r['predicted']:+.1%}" if r.get("predicted") is not None else "N/A"
            t5 = f"{r.get(5, 0):+.1%}" if 5 in r else "N/A"
            t10 = f"{r.get(10, 0):+.1%}" if 10 in r else "N/A"
            t20 = f"{r.get(20, 0):+.1%}" if 20 in r else "N/A"
            report_lines.append(f"│ {date_short}  │ {r['code']} │ {pred:>7} │ {t5:>7} │ {t10:>7} │ {t20:>8} │")

        report_lines.append("└──────────┴────────┴─────────┴─────────┴─────────┴──────────┘")
        report_lines.append("")

        # 汇总统计
        report_lines.append("汇总统计:")

        for period in [5, 10, 20]:
            values = [r[period] for r in buy_results if period in r]
            if values:
                hit_rate = sum(1 for v in values if v > 0) / len(values)
                avg_ret = np.mean(values)
                report_lines.append(f"  - T+{period:2d} 命中率: {hit_rate:.0%}，平均收益: {avg_ret:+.1%}")
            else:
                report_lines.append(f"  - T+{period:2d}: 数据不足")

        # 相关性
        pairs = [(r.get("predicted"), r.get(20)) for r in buy_results
                 if r.get("predicted") is not None and 20 in r]
        if len(pairs) >= 3:
            preds = [p[0] for p in pairs]
            actuals = [p[1] for p in pairs]
            corr = np.corrcoef(preds, actuals)[0, 1]
            report_lines.append(f"  - 预测 vs 实际相关性 (T+20): {corr:.2f}")
        else:
            report_lines.append("  - 预测 vs 实际相关性: 数据不足")
    else:
        days_span = (pd.to_datetime(max(dates)) - pd.to_datetime(min(dates))).days
        if days_span < 20:
            report_lines.append("信号数据积累不足，建议运行 20 个交易日后再查看绩效报告")
        else:
            report_lines.append("买入信号表现: 暂无足够后续数据评估")

    # ============ 止损/止盈信号表现 ============
    report_lines.append("")
    if all_sells:
        stop_loss = [s for s in all_sells if "止损" in s.get("reason", "")]
        take_profit = [s for s in all_sells if "止盈" in s.get("reason", "")]

        report_lines.append("止损/止盈信号表现:")

        if stop_loss:
            continued_down = 0
            for s in stop_loss:
                future = get_future_returns(s.get("code", ""), s["signal_date"], [10])
                if 10 in future and future[10] < 0:
                    continued_down += 1
            total = len(stop_loss)
            ratio = continued_down / total if total > 0 else 0
            report_lines.append(f"  - 止损信号数: {total}，止损后 T+10 继续下跌比例: {ratio:.0%}（{'说明止损有效' if ratio > 0.5 else '止损可能过早'}）")

        if take_profit:
            continued_up = 0
            for s in take_profit:
                future = get_future_returns(s.get("code", ""), s["signal_date"], [10])
                if 10 in future and future[10] > 0:
                    continued_up += 1
            total = len(take_profit)
            ratio = continued_up / total if total > 0 else 0
            report_lines.append(f"  - 止盈信号数: {total}，止盈后 T+10 继续上涨比例: {ratio:.0%}")
    else:
        report_lines.append("止损/止盈信号表现: 暂无卖出信号数据")

    report_lines.append("════════════════════════════")

    report = "\n".join(report_lines)
    print(report)

    # 推送
    if push:
        try:
            from alert.notify import send_to_all
            send_to_all("信号绩效报告", report)
            print("\n已推送到微信")
        except Exception as e:
            print(f"推送失败: {e}")

    return report


if __name__ == "__main__":
    push = "--push" in sys.argv
    run(push=push)
