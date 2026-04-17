"""
消息通知模块 - PushPlus 微信推送

使用方法:
1. 微信搜索公众号「PushPlus推送加」并关注
2. 关注后回复或扫码获取 token
3. 将 token 填入 config/settings.py 的 PUSHPLUS_TOKEN
"""

import requests
import logging

logger = logging.getLogger(__name__)

PUSHPLUS_API = "http://www.pushplus.plus/send"


def send_message(title: str, content: str, token: str, template: str = "markdown") -> bool:
    """
    通过 PushPlus 发送微信消息（单 token）

    Parameters
    ----------
    title : str   消息标题
    content : str  消息内容（支持 Markdown）
    token : str    PushPlus token
    template : str  模板类型: "markdown" / "html" / "txt"

    Returns
    -------
    bool: 是否发送成功
    """
    if not token or token == "YOUR_TOKEN_HERE":
        logger.warning("PushPlus token 未配置，跳过推送")
        return False

    payload = {
        "token": token,
        "title": title,
        "content": content,
        "template": template,
    }

    try:
        resp = requests.post(PUSHPLUS_API, json=payload, timeout=10)
        result = resp.json()
        if result.get("code") == 200:
            logger.info(f"推送成功: {title}")
            return True
        else:
            logger.error(f"推送失败: {result}")
            return False
    except Exception as e:
        logger.error(f"推送异常: {e}")
        return False


def send_to_all(title: str, content: str, template: str = "markdown") -> int:
    """
    推送到所有已配置的微信账号（PUSHPLUS_TOKENS 列表）

    Returns
    -------
    int: 成功推送的数量
    """
    from config.settings import PUSHPLUS_TOKENS
    success = 0
    for token in PUSHPLUS_TOKENS:
        if send_message(title, content, token, template):
            success += 1
    return success


def format_signal_message(signals_today: list, etf_pool: dict, tracker_summary: str) -> str:
    """
    格式化交易信号消息（Markdown 格式）

    Parameters
    ----------
    signals_today : list  今日信号列表 [{symbol, action, momentum, price}]
    etf_pool : dict       ETF 名称映射
    tracker_summary : str  持仓摘要
    """
    lines = ["## ETF轮动策略 - 交易信号\n"]

    if not signals_today:
        lines.append("**今日无操作**，继续持有当前仓位。\n")
    else:
        lines.append("**需要操作:**\n")
        for sig in signals_today:
            action_cn = "买入" if sig["action"] == "buy" else "卖出"
            emoji = "🟢" if sig["action"] == "buy" else "🔴"
            name = etf_pool.get(sig["symbol"], sig["symbol"])
            momentum = sig.get("momentum", 0)
            price = sig.get("price", "")
            price_str = f" | 现价: {price:.3f}" if price else ""
            lines.append(
                f"- {emoji} **{action_cn}** {name}({sig['symbol']})"
                f"{price_str} | 动量: {momentum:+.2%}"
            )

    if tracker_summary:
        lines.append(f"\n---\n{tracker_summary}")

    return "\n".join(lines)


def format_no_signal_message(tracker_summary: str) -> str:
    """格式化无信号时的消息"""
    lines = [
        "## ETF轮动策略 - 每日报告\n",
        "**今日无操作信号**，继续持有。\n",
    ]
    if tracker_summary:
        lines.append(f"---\n{tracker_summary}")
    return "\n".join(lines)
