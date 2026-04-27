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


def _get_decision_note() -> str:
    """读取今日计划中的决策摘要"""
    try:
        import json, os
        plan_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "sim_daily_plan.json"
        )
        if os.path.exists(plan_file):
            with open(plan_file, "r") as f:
                plan = json.load(f)
            return plan.get("decision_note", "")
    except Exception:
        pass
    return ""


def _humanize_reason(trade: dict) -> str:
    """
    将技术指标翻译成通俗易懂的理由
    优先用结构化 reason_data，无则降级用 reason 字符串正则解析
    """
    reason = trade.get("reason", "")
    if not reason:
        return ""

    name = trade.get("name", trade.get("symbol", ""))

    # 卖出理由保持原样
    if any(kw in reason for kw in ["止损", "止盈", "超时调仓", "调仓换股"]):
        return reason

    # 优先用结构化数据
    reason_data = trade.get("reason_data")
    if isinstance(reason_data, str):
        try:
            import json
            reason_data = json.loads(reason_data)
        except (json.JSONDecodeError, TypeError):
            reason_data = None

    from portfolio.reason_text import humanize_reason as _humanize
    result = _humanize(reason_data or {}, name=name, fallback_reason=reason)

    # 维度得分（如有）
    dim_scores = trade.get("dimension_scores")
    if dim_scores:
        dim_str = _format_dimension_scores(dim_scores, compact=True)
        if dim_str:
            result += f"\n    得分: {dim_str}"

    return result


def _ai_decision_summary(sells: list, buys: list, holdings: dict,
                         total_value: float, daily_ret: float) -> str:
    """
    调用 GLM-4-flash 生成通俗易懂的整体决策解读
    一次调用，涵盖所有买卖和无操作的解读
    """
    try:
        from config.settings import LLM_API_KEY, LLM_BASE_URL
        import requests
    except ImportError:
        return ""

    if not LLM_API_KEY:
        return ""

    # 构建上下文
    context_parts = []
    if sells:
        for t in sells:
            name = t.get("name", t.get("symbol", ""))
            profit = t.get("profit", 0)
            reason = t.get("reason", "")
            context_parts.append(f"卖出{name}: {reason}, 盈亏{profit:+,.0f}元")
    if buys:
        for t in buys:
            name = t.get("name", t.get("symbol", ""))
            reason = t.get("reason", "")
            context_parts.append(f"买入{name}: {reason}")
    if not sells and not buys:
        note = _get_decision_note()
        if note:
            context_parts.append(f"无操作: {note}")

    # 持仓状态
    if holdings:
        hold_str = ", ".join(
            f"{code}({info['shares']}股成本{info['avg_cost']:.2f})"
            for code, info in holdings.items()
        )
        context_parts.append(f"当前持仓: {hold_str}")

    context_parts.append(f"总资产{total_value:,.0f}元, 今日收益{daily_ret:+.2%}")

    prompt = f"""你是A股量化交易分析师。请用1-2句通俗易懂的话解读今日模拟盘操作决策，面向非专业投资者。

今日决策数据:
{chr(10).join(context_parts)}

要求:
1. 用大白话解释为什么买/卖/不动，不要用技术术语
2. 如果有买卖，说明核心逻辑；如果没操作，说明为什么按兵不动
3. 不要重复数据，只说结论和逻辑
4. 控制在80字以内"""

    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "glm-4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 200,
            },
            timeout=15,
        )
        result = resp.json()
        content = result["choices"][0]["message"].get("content", "").strip()
        return content
    except Exception as e:
        logger.warning(f"AI决策解读失败: {e}")
        return ""


def _get_holding_analysis() -> list:
    """从计划文件中读取持仓因子分析"""
    try:
        import json, os
        plan_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "sim_daily_plan.json"
        )
        if os.path.exists(plan_file):
            with open(plan_file, "r") as f:
                plan = json.load(f)
            return plan.get("holding_analysis", [])
    except Exception:
        pass
    return []


def _describe_holding_factors(factors: dict) -> str:
    """把持仓因子翻译成一句话描述"""
    if not factors:
        return ""

    parts = []

    # 动量
    mom = factors.get("mom_20d")
    if mom is not None:
        try:
            v = float(mom) * 100
            if v > 10:
                parts.append(f"强势(20日涨{v:.0f}%)")
            elif v > 0:
                parts.append(f"温和上涨(+{v:.0f}%)")
            elif v > -5:
                parts.append(f"小幅调整({v:.0f}%)")
            else:
                parts.append(f"弱势(20日跌{abs(v):.0f}%)")
        except (ValueError, TypeError):
            pass

    # RSI
    rsi = factors.get("rsi_14")
    if rsi is not None:
        try:
            v = float(rsi)
            if v > 70:
                parts.append(f"超买(RSI={v:.0f})")
            elif v < 30:
                parts.append(f"超卖(RSI={v:.0f})")
        except (ValueError, TypeError):
            pass

    # 估值
    pe = factors.get("pe_ttm")
    if pe is not None:
        try:
            v = float(pe)
            if v < 0:
                parts.append("亏损股")
            elif v < 20:
                parts.append(f"低估值(PE={v:.0f})")
            elif v > 50:
                parts.append(f"高估值(PE={v:.0f})")
        except (ValueError, TypeError):
            pass

    pb = factors.get("pb")
    if pb is not None:
        try:
            v = float(pb)
            if v < 1:
                parts.append("破净")
        except (ValueError, TypeError):
            pass

    return "，".join(parts) if parts else ""


def _format_dimension_scores(dim_scores: dict, compact: bool = False) -> str:
    """
    格式化三维度得分，展示每个因子的原始值和加减分

    compact=False (终端): 多行展示
    compact=True (微信): 单行展示
    """
    if not dim_scores:
        return ""

    if compact:
        # 微信推送: 单行精简版
        parts = []
        for dim in ["技术面", "基本面", "资金面"]:
            info = dim_scores.get(dim, {})
            score = info.get("score")
            items = info.get("items", [])
            if score is not None:
                item_str = ", ".join(
                    f"{it['name']}={it['value']}{'('+it['label']+')' if it.get('label') else ''}"
                    for it in items
                )
                parts.append(f"{dim}{int(score)}分({item_str})")
        return " | ".join(parts)

    # 终端: 多行展示，含加减分详情
    lines = []
    for dim in ["技术面", "基本面", "资金面"]:
        info = dim_scores.get(dim, {})
        score = info.get("score")
        items = info.get("items", [])
        if score is None:
            continue
        item_parts = []
        for it in items:
            s = f"{it['name']}={it['value']}"
            if it.get("label"):
                s += f"({it['label']})"
            if it.get("delta") and it["delta"] != 0:
                s += f"{'+' if it['delta']>0 else ''}{it['delta']}分"
            item_parts.append(s)
        detail = ", ".join(item_parts) if item_parts else "基准50分"
        lines.append(f"    {dim}{int(score)}分: {detail}")
    return "\n".join(lines)


def _format_sentiment(sentiment: dict, compact: bool = False) -> str:
    """格式化个股情绪分析结果"""
    if not sentiment or not sentiment.get("top_news"):
        return ""

    score = sentiment.get("score", 0)
    news_count = sentiment.get("news_count", 0)
    top_news = sentiment.get("top_news", [])

    if score > 0.3:
        tag = "利好"
    elif score < -0.3:
        tag = "利空"
    else:
        tag = "中性"

    if compact:
        # 微信: 一行
        news_str = "; ".join(
            f"{'利好' if n['sentiment'] > 0 else '利空'}:{n['title'][:15]}"
            for n in top_news[:2]
        )
        return f"情绪{tag}({score:+.1f}) {news_str}" if news_str else f"情绪{tag}({score:+.1f})"
    else:
        # 终端: 多行
        lines = [f"    情绪: {tag}({score:+.1f}), {news_count}条相关新闻"]
        for n in top_news[:3]:
            s = "利好" if n["sentiment"] > 0 else "利空"
            lines.append(f"      [{s}] {n['title']}")
        return "\n".join(lines)


def _ai_holding_summary(holding_analysis: list) -> str:
    """调用 GLM-4-flash 生成持仓整体解读"""
    try:
        from config.settings import LLM_API_KEY, LLM_BASE_URL
        import requests
    except ImportError:
        return ""

    if not LLM_API_KEY:
        return ""

    # 构建持仓描述
    hold_parts = []
    for h in holding_analysis:
        name = h.get("name", h["code"])
        pnl = h.get("pnl_pct", 0)
        days = h.get("days_held", 0)
        fac = h.get("factors", {})
        fac_str = ", ".join(f"{k}={v:.2f}" if v is not None else f"{k}=N/A"
                           for k, v in fac.items())
        desc = f"{name}: 收益{pnl:+.1f}%, 持有{days}日, {fac_str}"
        # 加入情绪数据
        sent = h.get("sentiment", {})
        if sent and sent.get("top_news"):
            score = sent.get("score", 0)
            top = sent["top_news"][0]["title"][:30]
            desc += f", 情绪{score:+.1f}({top})"
        hold_parts.append(desc)

    prompt = f"""你是A股量化分析师。请用2-3句话点评以下持仓组合的整体状态，面向普通投资者。

持仓:
{chr(10).join(hold_parts)}

要求:
1. 点评每只股票的表现(涨跌、估值、风险)
2. 给出整体持仓健康度判断
3. 用大白话，不使用技术术语
4. 控制在100字以内"""

    try:
        resp = requests.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {LLM_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "glm-4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 200,
            },
            timeout=15,
        )
        result = resp.json()
        return result["choices"][0]["message"].get("content", "").strip()
    except Exception as e:
        logger.warning(f"AI持仓解读失败: {e}")
        return ""


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
            human_reason = _humanize_reason(t)
            lines.append(
                f"  {t.get('name', '')}({t['symbol']})"
                f" {t['shares']}股@{t['price']:.2f}"
                f" = {t['amount']:,.0f}元 {profit_str}"
            )
            if human_reason:
                lines.append(f"    理由: {human_reason}")

    if buys:
        lines.append("")
        lines.append("--- 买入 ---")
        for t in buys:
            human_reason = _humanize_reason(t)
            lines.append(
                f"  {t.get('name', '')}({t['symbol']})"
                f" {t['shares']}股@{t['price']:.2f}"
                f" = {t['amount']:,.0f}元"
            )
            if human_reason:
                lines.append(f"    理由: {human_reason}")

    if not sells and not buys:
        lines.append("  今日无交易")
        note = _get_decision_note()
        if note:
            lines.append(f"  决策理由: {note}")

    # AI 整体解读
    ai_summary = _ai_decision_summary(sells, buys, holdings, total_value, daily_ret)
    if ai_summary:
        lines.append("")
        lines.append(f"  AI解读: {ai_summary}")

    # 持仓明细
    if holdings:
        lines.append("")
        lines.append("--- 持仓 ---")
        holding_analysis = _get_holding_analysis()
        for code, info in holdings.items():
            ha = next((h for h in holding_analysis if h["code"] == code), None)
            if ha:
                pnl_str = f"{ha['pnl_pct']:+.1f}%({ha['pnl_amount']:+,.0f}元)"
                days_str = f"持有{ha['days_held']}日"
                lines.append(
                    f"  {ha['name']}({code}): {info['shares']}股"
                    f" @{info['avg_cost']:.3f}→{ha['price']:.2f}"
                    f" | {pnl_str} | {days_str}"
                )
                # 关键因子一句话
                factor_desc = _describe_holding_factors(ha.get("factors", {}))
                if factor_desc:
                    lines.append(f"    {factor_desc}")
                # 维度得分拆解
                dim_str = _format_dimension_scores(ha.get("dimension_scores", {}))
                if dim_str:
                    lines.append(dim_str)
                # 情绪分析
                sent_str = _format_sentiment(ha.get("sentiment", {}))
                if sent_str:
                    lines.append(sent_str)
            else:
                lines.append(
                    f"  {code}: {info['shares']}股 @ {info['avg_cost']:.3f}"
                    f" (买入 {info.get('buy_date', '?')})"
                )

        # AI 持仓解读
        if holding_analysis:
            ai_hold = _ai_holding_summary(holding_analysis)
            if ai_hold:
                lines.append(f"  持仓解读: {ai_hold}")

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
    """微信推送Markdown格式，结构清晰便于手机阅读"""
    lines = [f"**模拟盘日报 {date}**"]

    if sells:
        lines.append("\n**卖出**")
        lines.append("---")
        for t in sells:
            profit_str = f"盈亏{t['profit']:+,.0f}元" if t.get("profit") else ""
            human_reason = _humanize_reason(t)
            lines.append(f"**{t.get('name', '')}**({t['symbol']}) {t['shares']}股@{t['price']:.2f}")
            if profit_str:
                lines.append(profit_str)
            if human_reason:
                lines.append(f"> {human_reason}")
            lines.append("")

    if buys:
        lines.append("**买入**")
        lines.append("---")
        for t in buys:
            human_reason = _humanize_reason(t)
            lines.append(f"**{t.get('name', '')}**({t['symbol']}) {t['shares']}股@{t['price']:.2f} = {t['amount']:,.0f}元")
            if human_reason:
                lines.append(f"> {human_reason}")
            lines.append("")

    if not sells and not buys:
        lines.append("")
        note = _get_decision_note()
        if note:
            lines.append(f"**今日无交易** — {note}")
        else:
            lines.append("**今日无交易**")

    lines.append("---")
    lines.append(f"现金 {cash:,.0f} | 总资产 {total_value:,.0f}")
    lines.append(f"今日 {daily_ret:+.2%} | 累计 {total_ret:+.2%}")

    # AI 整体解读
    ai_summary = _ai_decision_summary(sells, buys, holdings, total_value, daily_ret)
    if ai_summary:
        lines.append(f"\n> AI解读: {ai_summary}")

    # 持仓分析
    holding_analysis = _get_holding_analysis()
    if holding_analysis:
        lines.append("\n**持仓**")
        lines.append("---")
        for ha in holding_analysis:
            pnl_str = f"{ha['pnl_pct']:+.1f}%({ha['pnl_amount']:+,.0f}元)"
            days_str = f"持有{ha.get('days_held', '?')}日"
            lines.append(
                f"**{ha['name']}**({ha['code']})"
                f" {holdings.get(ha['code'], {}).get('shares', '?')}股"
                f" | {pnl_str} | {days_str}"
            )
            dim_str = _format_dimension_scores(ha.get("dimension_scores", {}), compact=True)
            sent_str = _format_sentiment(ha.get("sentiment", {}), compact=True)
            if dim_str:
                lines.append(f"  {dim_str}")
            if sent_str:
                lines.append(f"  {sent_str}")
            lines.append("")
        ai_hold = _ai_holding_summary(holding_analysis)
        if ai_hold:
            lines.append(f"> {ai_hold}")

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
