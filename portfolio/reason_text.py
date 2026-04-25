"""
统一理由文案生成 — 结构化 dict 输入，无正则

所有模块（终端/微信/模拟盘日报）共用此函数。
"""

import re
from typing import Optional


def humanize_reason(reason_data: dict, name: str = "",
                    fallback_reason: str = "") -> str:
    """
    将结构化因子数据翻译成通俗易懂的理由

    Parameters
    ----------
    reason_data : dict
        结构化因子数据，包含 factor_rank, ml_rank, in_both, key_factors, predicted_return 等
    name : str
        股票名称（前缀）
    fallback_reason : str
        降级用原始 reason 字符串（当 reason_data 为空时使用 legacy 正则解析）
    """
    if not reason_data:
        # legacy fallback: 用正则解析原始字符串
        if fallback_reason:
            return _legacy_humanize(fallback_reason, name)
        return ""

    parts = []
    factor_rank = reason_data.get("factor_rank")
    ml_rank = reason_data.get("ml_rank")
    in_both = reason_data.get("in_both", False)

    # 排名信息
    if factor_rank is not None and ml_rank is not None:
        fr, mr = int(factor_rank), int(ml_rank)
        if in_both:
            parts.append("多因子和ML模型均排名靠前，信号强烈")
        elif fr <= 20:
            parts.append(f"多因子排名第{fr}，技术面优势明显")
        elif mr <= 20:
            parts.append(f"ML模型预测排名第{mr}，看好后续走势")
        else:
            parts.append(f"多因子第{fr}、ML第{mr}")

    # 关键因子翻译
    kf = reason_data.get("key_factors", {})

    # 动量
    mom_20d = kf.get("mom_20d")
    if mom_20d is not None:
        try:
            v = float(mom_20d) * 100
            if v > 15:
                parts.append(f"短期强势(20日涨{v:.0f}%)")
            elif v > 5:
                parts.append(f"温和上涨(20日涨{v:.0f}%)")
            elif v < -10:
                parts.append(f"短期弱势(20日跌{abs(v):.0f}%)")
        except (ValueError, TypeError):
            pass

    # 估值
    pe = kf.get("pe_ttm")
    if pe is not None:
        try:
            v = float(pe)
            if v < 0:
                parts.append("亏损股")
            elif v < 15:
                parts.append(f"低估值(PE仅{v:.0f})")
            elif v > 50:
                parts.append(f"估值偏高(PE={v:.0f})")
        except (ValueError, TypeError):
            pass

    pb = kf.get("pb")
    if pb is not None:
        try:
            v = float(pb)
            if v < 1:
                parts.append(f"破净(PB={v:.1f})")
            elif v < 3:
                parts.append(f"估值合理(PB={v:.1f})")
        except (ValueError, TypeError):
            pass

    # ML 预测收益
    pred_ret = reason_data.get("predicted_return")
    if pred_ret is not None:
        try:
            v = float(pred_ret) * 100
            if v > 3:
                parts.append(f"模型预测看涨(+{v:.0f}%)")
            elif v < -3:
                parts.append(f"模型预测有风险({v:.0f}%)")
        except (ValueError, TypeError):
            pass

    if parts:
        prefix = f"{name}：" if name else ""
        return f"{prefix}{'，'.join(parts)}"
    return fallback_reason


# ---- legacy: 正则解析旧格式 reason 字符串（仅作 fallback） ----

def _legacy_humanize(reason: str, name: str = "") -> str:
    """用正则解析自家拼出的字符串（legacy fallback）"""
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

    if parts:
        prefix = f"{name}：" if name else ""
        return f"{prefix}{'，'.join(parts)}"
    return reason
