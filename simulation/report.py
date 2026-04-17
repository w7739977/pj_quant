"""
模拟交易报告模块

功能:
  - 日报: 当日操作 + 收益 + 持仓
  - 周报: 胜率、最大回撤、夏普比率
  - 推送消息格式化
"""

import logging
from datetime import datetime, timedelta
from simulation.trade_log import (
    get_today_trades, get_trades, get_snapshots,
    get_latest_snapshot, load_sim_portfolio,
)

logger = logging.getLogger(__name__)


def daily_report(push_format: bool = False) -> str:
    """
    生成当日报告

    Parameters
    ----------
    push_format : bool  True=微信推送Markdown, False=终端输出
    """
    trades = get_today_trades()
    portfolio = load_sim_portfolio()
    snapshot = get_latest_snapshot()

    today = datetime.now().strftime("%Y-%m-%d")
    sells = [t for t in trades if t["side"] == "sell"]
    buys = [t for t in trades if t["side"] == "buy"]

    total_value = snapshot["total_value"] if snapshot else 0
    daily_ret = snapshot["daily_return"] if snapshot else 0
    total_ret = snapshot["total_return"] if snapshot else 0
    cash = portfolio.get("cash", 0)
    holdings = portfolio.get("holdings", {})

    if push_format:
        return _format_push_daily(today, sells, buys, cash,
                                  total_value, daily_ret, total_ret, holdings)
    else:
        return _format_terminal_daily(today, sells, buys, cash,
                                      total_value, daily_ret, total_ret, holdings)


def _format_terminal_daily(date, sells, buys, cash, total_value,
                           daily_ret, total_ret, holdings) -> str:
    """终端格式日报"""
    lines = [
        "=" * 50,
        f"模拟盘日报 {date}",
        "=" * 50,
    ]

    # 成交汇总
    if sells:
        lines.append("")
        lines.append("--- 卖出 ---")
        for t in sells:
            profit_str = f"{t['profit']:+,.0f}元" if t.get("profit") else ""
            lines.append(
                f"  {t.get('name', '')}({t['symbol']})"
                f" {t['shares']}股@{t['price']:.2f}"
                f" = {t['amount']:,.0f}元 {profit_str}"
                f" {t.get('reason', '')}"
            )

    if buys:
        lines.append("")
        lines.append("--- 买入 ---")
        for t in buys:
            lines.append(
                f"  {t.get('name', '')}({t['symbol']})"
                f" {t['shares']}股@{t['price']:.2f}"
                f" = {t['amount']:,.0f}元"
                f" {t.get('reason', '')}"
            )

    if not sells and not buys:
        lines.append("  今日无交易")

    # 持仓明细
    if holdings:
        lines.append("")
        lines.append("--- 持仓 ---")
        for code, info in holdings.items():
            lines.append(
                f"  {code}: {info['shares']}股 @ {info['avg_cost']:.3f}"
                f" (买入 {info.get('buy_date', '?')})"
            )

    # 资产概览
    lines.extend([
        "",
        "-" * 50,
        f"现金: {cash:,.0f}元",
        f"总资产: {total_value:,.0f}元",
        f"今日收益: {daily_ret:+.2%}",
        f"累计收益: {total_ret:+.2%}",
        "=" * 50,
    ])
    return "\n".join(lines)


def _format_push_daily(date, sells, buys, cash, total_value,
                       daily_ret, total_ret, holdings) -> str:
    """微信推送Markdown格式"""
    lines = [f"## 模拟盘日报 {date}\n"]

    if sells:
        lines.append("**卖出:**")
        for t in sells:
            profit_str = f" 盈亏{t['profit']:+,.0f}" if t.get("profit") else ""
            lines.append(
                f"- {t.get('name', '')}({t['symbol']})"
                f" {t['shares']}股@{t['price']:.2f}"
                f"{profit_str}"
            )

    if buys:
        lines.append("**买入:**")
        for t in buys:
            lines.append(
                f"- {t.get('name', '')}({t['symbol']})"
                f" {t['shares']}股@{t['price']:.2f}"
                f" = {t['amount']:,.0f}元"
            )

    if not sells and not buys:
        lines.append("**今日无交易**")

    lines.append(f"\n现金{cash:,.0f} | 资产{total_value:,.0f}")
    lines.append(f"今日{daily_ret:+.2%} | 累计{total_ret:+.2%}")
    return "\n".join(lines)


def weekly_report(push_format: bool = False) -> str:
    """
    生成周报（最近5个交易日快照）

    包含: 胜率、最大回撤、夏普比率、交易统计
    """
    snapshots = get_snapshots(limit=5)
    if not snapshots:
        return "暂无数据，运行模拟盘后再查看"

    # 获取最近一周的交易
    end = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    trades = get_trades(start_date=start, end_date=end)
    sells = [t for t in trades if t["side"] == "sell"]

    # 计算统计
    stats = _calc_stats(snapshots, sells)

    if push_format:
        return _format_push_weekly(snapshots, stats, trades)
    else:
        return _format_terminal_weekly(snapshots, stats, trades)


def _calc_stats(snapshots: list, sells: list) -> dict:
    """计算绩效统计"""
    if not snapshots:
        return {}

    daily_returns = [s["daily_return"] for s in reversed(snapshots)
                     if s.get("daily_return") is not None]

    # 总收益
    latest = snapshots[0]
    total_return = latest.get("total_return", 0)

    # 胜率
    win_count = sum(1 for t in sells if t.get("profit", 0) > 0)
    lose_count = sum(1 for t in sells if t.get("profit", 0) < 0)
    total_trades = win_count + lose_count
    win_rate = win_count / total_trades if total_trades > 0 else 0

    # 最大回撤
    max_dd = 0
    peak = 0
    for s in reversed(snapshots):
        tv = s.get("total_value", 0)
        if tv > peak:
            peak = tv
        dd = (peak - tv) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # 夏普比率（简化：假设无风险利率0，日收益年化）
    if len(daily_returns) > 1:
        import numpy as np
        arr = np.array(daily_returns)
        sharpe = float(arr.mean() / arr.std() * (252 ** 0.5)) if arr.std() > 0 else 0
    else:
        sharpe = 0

    # 平均持仓天数
    profits = [t.get("profit", 0) for t in sells]
    avg_profit = sum(profits) / len(profits) if profits else 0

    return {
        "total_return": total_return,
        "win_rate": win_rate,
        "win_count": win_count,
        "lose_count": lose_count,
        "total_trades": total_trades,
        "max_drawdown": max_dd,
        "sharpe": sharpe,
        "avg_profit": avg_profit,
        "total_days": len(snapshots),
    }


def _format_terminal_weekly(snapshots, stats, trades) -> str:
    lines = [
        "=" * 50,
        f"模拟盘周报 (近{stats.get('total_days', 0)}个交易日)",
        "=" * 50,
        "",
        "--- 绩效概览 ---",
        f"  累计收益: {stats.get('total_return', 0):+.2%}",
        f"  胜率: {stats.get('win_rate', 0):.1%}"
        f" ({stats.get('win_count', 0)}胜 {stats.get('lose_count', 0)}负"
        f" / {stats.get('total_trades', 0)}笔)",
        f"  最大回撤: {stats.get('max_drawdown', 0):.2%}",
        f"  夏普比率: {stats.get('sharpe', 0):.2f}",
        f"  平均盈利: {stats.get('avg_profit', 0):+,.0f}元",
    ]

    # 每日净值
    lines.extend(["", "--- 每日净值 ---"])
    for s in snapshots:
        lines.append(
            f"  {s['date']}  资产{s['total_value']:>10,.0f}"
            f"  日收益{s.get('daily_return', 0):>+7.2%}"
        )

    lines.append("=" * 50)
    return "\n".join(lines)


def _format_push_weekly(snapshots, stats, trades) -> str:
    lines = [
        f"## 模拟盘周报\n",
        f"**累计收益:** {stats.get('total_return', 0):+.2%}",
        f"**胜率:** {stats.get('win_rate', 0):.1%}"
        f" ({stats.get('win_count', 0)}胜{stats.get('lose_count', 0)}负)",
        f"**最大回撤:** {stats.get('max_drawdown', 0):.2%}",
        f"**夏普:** {stats.get('sharpe', 0):.2f}",
        "",
    ]
    for s in snapshots[:5]:
        lines.append(f"- {s['date']} 资产{s['total_value']:,.0f} {s.get('daily_return', 0):+.2%}")
    return "\n".join(lines)


def format_sim_push_message(report_text: str, title: str = None) -> tuple:
    """
    格式化推送消息

    Returns
    -------
    (title, content)
    """
    if title is None:
        title = f"模拟盘 ({datetime.now().strftime('%m-%d')})"
    return title, report_text
