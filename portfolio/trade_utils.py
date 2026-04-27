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


def humanize_reason(reason: str, name: str = "") -> str:
    """将技术指标 reason 翻译成通俗中文"""
    if not reason:
        return ""

    # 卖出理由已经是中文
    if any(kw in reason for kw in ["止损", "止盈", "超时调仓", "调仓换股"]):
        return reason

    parts = []

    # 排名
    factor_match = re.search(r"因子#(\d+)", reason)
    ml_match = re.search(r"ML#(\d+)", reason)
    both = "★双重确认" in reason

    if factor_match and ml_match:
        fr, mr = int(factor_match.group(1)), int(ml_match.group(1))
        if both:
            parts.append("多因子和ML模型均排名靠前，信号强烈")
        elif fr <= 20:
            parts.append(f"多因子排名第{fr}，技术面优势明显")
        elif mr <= 20:
            parts.append(f"ML模型预测排名第{mr}，看好后续走势")
        else:
            parts.append(f"多因子第{fr}、ML第{mr}")

    # 因子翻译
    factor_labels = {
        "mom_20d": True,
        "pe_ttm": False,
        "pb": False,
    }
    for key, is_pct in factor_labels.items():
        m = re.search(rf"{key}:([+-]?\d+\.?\d*%?)", reason)
        if m:
            val = m.group(1)
            if key == "pe_ttm":
                try:
                    v = float(val)
                    if v < 0:
                        parts.append("亏损股")
                    elif v < 15:
                        parts.append(f"低估值(PE仅{v:.0f})")
                    elif v > 50:
                        parts.append(f"估值偏高(PE={v:.0f})")
                except ValueError:
                    pass
            elif key == "pb":
                try:
                    v = float(val)
                    if v < 1:
                        parts.append(f"破净(PB={v:.1f})")
                    elif v < 3:
                        parts.append(f"估值合理(PB={v:.1f})")
                except ValueError:
                    pass
            elif key == "mom_20d":
                try:
                    v = float(val.replace("%", ""))
                    if v > 15:
                        parts.append(f"短期强势(20日涨{v:.0f}%)")
                    elif v > 5:
                        parts.append(f"温和上涨(20日涨{v:.0f}%)")
                    elif v < -10:
                        parts.append(f"短期弱势(20日跌{abs(v):.0f}%)")
                except ValueError:
                    pass

    # ML预测
    pred_match = re.search(r"预测20日收益:([+-]?\d+\.?\d*%?)", reason)
    if pred_match:
        try:
            v = float(pred_match.group(1).replace("%", ""))
            if v > 3:
                parts.append(f"模型预测看涨(+{v:.0f}%)")
            elif v < -3:
                parts.append(f"模型预测有风险({v:.0f}%)")
        except ValueError:
            pass

    # 主力资金流向
    flow_match = re.search(r"资金:(.+?)(?:\n|$)", reason)
    if flow_match:
        flow_str = flow_match.group(1)
        # 匹配 "主力净流入+5.9亿" 或 "主力净流出7852万"
        mf_match = re.search(r"主力净(流入|流出)([\d.]+[亿万])", flow_str)
        if mf_match:
            direction = mf_match.group(1)
            mf_amount = mf_match.group(2)
            if direction == "流入":
                detail_parts = []
                elg_m = re.search(r"超大单([+-]?[\d.]+[亿万])", flow_str)
                lg_m = re.search(r"(?<!超)大单([+-]?[\d.]+[亿万])", flow_str)
                if elg_m:
                    detail_parts.append(f"超大单{elg_m.group(1)}")
                if lg_m:
                    detail_parts.append(f"大单{lg_m.group(1)}")
                if detail_parts:
                    parts.append(f"主力资金净流入{mf_amount}，{', '.join(detail_parts)}，资金积极做多")
                else:
                    parts.append(f"主力资金净流入{mf_amount}，资金看好")
            else:
                parts.append(f"主力资金净流出{mf_amount}，注意风险")

    if parts:
        prefix = f"{name}：" if name else ""
        return f"{prefix}{'，'.join(parts)}"
    return reason

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
            reason_str = humanize_reason(a.get('reason', ''), a.get('name', ''))
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
                lines.append(f"     {humanize_reason(a['reason'], a.get('name', ''))}")

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
            reason_str = humanize_reason(a.get('reason', ''), a.get('name', ''))
            lines.append(f"**{a.get('name', '')}**({a['code']}) {a['shares']}股@{a['price']:.2f}")
            lines.append(f"盈亏 {pnl}")
            if reason_str:
                lines.append(f"> {reason_str}")
            lines.append("")

    if buy_actions:
        lines.append("**买入**")
        lines.append("---")
        for a in buy_actions:
            reason_str = humanize_reason(a.get('reason', ''), a.get('name', ''))
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
