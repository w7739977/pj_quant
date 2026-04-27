"""
实盘交易工具函数

A股交易规则:
  - 买入必须为 100 股整手
  - 佣金: 万2.5，最低 5 元
  - 印花税: 卖出 0.1%
  - 过户费: 0.001%

板块限制:
  - 可买: 主板(000/001/002/003/600/601/603/605) + 创业板(300)
  - 不可买: 科创板(688) + 北交所(8xx/4xx) + B股(900/200)
"""

import re
from config.settings import (
    LIVE_COMMISSION_RATE, LIVE_MIN_COMMISSION,
    LIVE_STAMP_TAX_RATE, LIVE_TRANSFER_FEE_RATE,
)


def humanize_reason(reason: str, name: str = "", reason_data: dict = None) -> str:
    """优先用 reason_data dict，无则降级用 reason 字符串正则解析"""
    from portfolio.reason_text import humanize_reason as _humanize
    return _humanize(reason_data or {}, name=name, fallback_reason=reason)

# 可交易代码前缀（正则）
_TRADEABLE_RE = re.compile(r"^(000|001|002|003|300|600|601|603|605)\d{3}$")


def is_tradeable(code: str) -> bool:
    """检查股票代码是否可交易（排除科创板、北交所、B股）"""
    return bool(_TRADEABLE_RE.match(code))


def calc_shares(capital: float, price: float) -> dict:
    """
    计算可买入股数（100股整手）

    Returns
    -------
    dict: {shares, amount, remaining}
    """
    if price <= 0 or capital <= 0:
        return {"shares": 0, "amount": 0.0, "remaining": capital}

    raw = capital / price
    lots = int(raw // 100)
    shares = lots * 100

    if shares < 100:
        return {"shares": 0, "amount": 0.0, "remaining": capital}

    amount = shares * price
    return {
        "shares": shares,
        "amount": round(amount, 2),
        "remaining": round(capital - amount, 2),
    }


def estimate_buy_cost(amount: float) -> float:
    """估算买入成本（佣金 + 过户费）"""
    commission = max(amount * LIVE_COMMISSION_RATE, LIVE_MIN_COMMISSION)
    transfer = amount * LIVE_TRANSFER_FEE_RATE
    return round(commission + transfer, 2)


def estimate_sell_cost(amount: float) -> float:
    """估算卖出成本（佣金 + 印花税 + 过户费）"""
    commission = max(amount * LIVE_COMMISSION_RATE, LIVE_MIN_COMMISSION)
    stamp = amount * LIVE_STAMP_TAX_RATE
    transfer = amount * LIVE_TRANSFER_FEE_RATE
    return round(commission + stamp + transfer, 2)


def format_checklist(sell_actions: list, buy_actions: list, summary: dict) -> str:
    """
    生成手机端友好的操作清单

    Parameters
    ----------
    sell_actions : [{code, name, shares, price, amount, reason, pnl, pnl_pct}]
    buy_actions : [{code, name, shares, price, amount, reason}]
    summary : {total_value, cash, total_pnl, total_pnl_pct}
    """
    lines = []

    # 头部
    lines.append("=" * 40)
    lines.append(f"今日操作清单")
    lines.append("=" * 40)

    if sell_actions:
        lines.append("")
        lines.append("--- 先卖 ---")
        for i, a in enumerate(sell_actions, 1):
            lines.append(
                f"  {i}. {a.get('name', '')}({a['code']})"
                f" {a['shares']}股 @ {a['price']:.2f}"
                f" = {a['amount']:,.0f}元"
            )
            pnl_str = f"{a['pnl']:+,.0f}元 ({a['pnl_pct']:+.1f}%)"
            reason_str = humanize_reason(
                a.get('reason', ''), a.get('name', ''),
                reason_data=a.get('reason_data'),
            )
            lines.append(f"     盈亏: {pnl_str} {reason_str}")

    if buy_actions:
        lines.append("")
        lines.append("--- 后买 ---")
        for i, a in enumerate(buy_actions, 1):
            lines.append(
                f"  {i}. {a.get('name', '')}({a['code']})"
                f" {a['shares']}股({a['shares']//100}手)"
                f" @ {a['price']:.2f}"
                f" = {a['amount']:,.0f}元"
            )
            if a.get("reason"):
                lines.append(f"     {humanize_reason(a['reason'], a.get('name', ''), reason_data=a.get('reason_data'))}")

    if not sell_actions and not buy_actions:
        lines.append("")
        lines.append("  今日无操作，继续持有")

    lines.append("")
    lines.append("-" * 40)
    lines.append(f"可用资金: {summary.get('cash', 0):,.0f}元")
    lines.append(f"总资产: {summary.get('total_value', 0):,.0f}元")
    pnl = summary.get("total_pnl", 0)
    pct = summary.get("total_pnl_pct", 0)
    lines.append(f"总盈亏: {pnl:+,.0f}元 ({pct:+.1f}%)")
    lines.append("=" * 40)

    return "\n".join(lines)


def format_push_message(sell_actions: list, buy_actions: list, summary: dict) -> str:
    """生成微信推送格式（Markdown），结构清晰便于手机阅读"""
    lines = []

    if sell_actions:
        lines.append("**卖出**")
        lines.append("---")
        for a in sell_actions:
            pnl = f"{a['pnl']:+,.0f}元({a['pnl_pct']:+.1f}%)"
            reason_str = humanize_reason(
                a.get('reason', ''), a.get('name', ''),
                reason_data=a.get('reason_data'),
            )
            lines.append(f"**{a.get('name', '')}**({a['code']}) {a['shares']}股@{a['price']:.2f}")
            lines.append(f"盈亏 {pnl}")
            if reason_str:
                lines.append(f"> {reason_str}")
            lines.append("")

    if buy_actions:
        lines.append("**买入**")
        lines.append("---")
        for a in buy_actions:
            reason_str = humanize_reason(
                a.get('reason', ''), a.get('name', ''),
                reason_data=a.get('reason_data'),
            )
            lines.append(
                f"**{a.get('name', '')}**({a['code']})"
                f" {a['shares']}股({a['shares']//100}手)"
                f"@{a['price']:.2f} = {a['amount']:,.0f}元"
            )
            if reason_str:
                lines.append(f"> {reason_str}")
            lines.append("")

    if not sell_actions and not buy_actions:
        lines.append("**今日无操作，继续持有**")

    cash = summary.get("cash", 0)
    total = summary.get("total_value", 0)
    pnl = summary.get("total_pnl", 0)
    pct = summary.get("total_pnl_pct", 0)
    lines.append("---")
    lines.append(f"资金 {cash:,.0f} | 总资产 {total:,.0f} | 盈亏 {pnl:+,.0f}({pct:+.1f}%)")

    return "\n".join(lines)
