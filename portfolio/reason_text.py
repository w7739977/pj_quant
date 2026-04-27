"""
统一理由文案生成 — 结构化 dict 输入，无正则

所有模块（终端/微信/模拟盘日报）共用此函数，避免回到"先字符串化再正则反解析"反模式。
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
        结构化因子数据，含 factor_rank / ml_rank / in_both / key_factors /
        predicted_return / capital_flow 等
    name : str
        股票名称（前缀）
    fallback_reason : str
        降级用原始 reason 字符串（当 reason_data 为空时调用 legacy 正则）
    """
    if not reason_data:
        if fallback_reason:
            return _legacy_humanize(fallback_reason, name)
        return ""

    parts = []

    # 排名
    factor_rank = reason_data.get("factor_rank")
    ml_rank = reason_data.get("ml_rank")
    in_both = reason_data.get("in_both", False)
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

    # 关键因子
    kf = reason_data.get("key_factors", {})

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

    # 主力资金流向（M5: 流入流出都展示明细）
    cf = reason_data.get("capital_flow")
    if cf:
        flow_part = _format_capital_flow(cf)
        if flow_part:
            parts.append(flow_part)

    if parts:
        prefix = f"{name}：" if name else ""
        return f"{prefix}{'，'.join(parts)}"
    return fallback_reason


def _format_capital_flow(cf: dict) -> str:
    """格式化资金流向，流入流出都展示超大单/大单明细"""
    mf = cf.get("net_mf_amount", 0) or 0
    elg = cf.get("elg_net", 0) or 0
    lg = cf.get("lg_net", 0) or 0

    direction = "净流入" if mf >= 0 else "净流出"
    main_str = f"主力{direction}{_fmt_amount(abs(mf))}"

    detail_parts = []
    if abs(elg) >= 1:
        sign = "+" if elg >= 0 else "-"
        detail_parts.append(f"超大单{sign}{_fmt_amount(abs(elg))}")
    if abs(lg) >= 1:
        sign = "+" if lg >= 0 else "-"
        detail_parts.append(f"大单{sign}{_fmt_amount(abs(lg))}")

    if detail_parts:
        suffix = "资金积极做多" if mf >= 0 else "注意资金抛压"
        return f"{main_str}({', '.join(detail_parts)})，{suffix}"
    suffix = "资金看好" if mf >= 0 else "注意风险"
    return f"{main_str}，{suffix}"


def _fmt_amount(wan_yuan: float) -> str:
    """万元 → 可读金额（无符号）"""
    if wan_yuan >= 10000:
        return f"{wan_yuan / 10000:.1f}亿"
    elif wan_yuan >= 100:
        return f"{wan_yuan:.0f}万"
    return f"{wan_yuan:.1f}万"


# ---- legacy: 正则解析旧格式 reason 字符串（仅作 fallback）----

def _legacy_humanize(reason: str, name: str = "") -> str:
    """用正则解析自家拼出的字符串（fallback，不应是主路径）"""
    if not reason:
        return ""

    if any(kw in reason for kw in ["止损", "止盈", "超时调仓", "调仓换股"]):
        return reason

    parts = []

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

    for key in ("mom_20d", "pe_ttm", "pb"):
        m = re.search(rf"{key}:([+-]?\d+\.?\d*%?)", reason)
        if not m:
            continue
        val = m.group(1)
        if key == "mom_20d":
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
        elif key == "pe_ttm":
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

    # 资金流（legacy 也补全流出明细）
    flow_match = re.search(r"资金:(.+?)(?:\n|$)", reason)
    if flow_match:
        flow_str = flow_match.group(1)
        mf_m = re.search(r"主力净(流入|流出)([\d.]+[亿万])", flow_str)
        if mf_m:
            direction, amount = mf_m.group(1), mf_m.group(2)
            elg_m = re.search(r"超大单([+-]?[\d.]+[亿万])", flow_str)
            lg_m = re.search(r"(?<!超)大单([+-]?[\d.]+[亿万])", flow_str)
            details = []
            if elg_m:
                details.append(f"超大单{elg_m.group(1)}")
            if lg_m:
                details.append(f"大单{lg_m.group(1)}")
            suffix = "资金积极做多" if direction == "流入" else "注意资金抛压"
            if details:
                parts.append(f"主力{direction}{amount}({', '.join(details)})，{suffix}")
            else:
                parts.append(f"主力{direction}{amount}，{suffix}")

    if parts:
        prefix = f"{name}：" if name else ""
        return f"{prefix}{'，'.join(parts)}"
    return reason
