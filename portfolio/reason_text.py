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
    sub_lines = []  # 维度详情行，分行展示

    # ---- 排名 + 综合得分 ----
    factor_rank = reason_data.get("factor_rank")
    ml_rank = reason_data.get("ml_rank")
    in_both = reason_data.get("in_both", False)
    final_score = reason_data.get("final_score")

    header_parts = []
    if factor_rank is not None and ml_rank is not None:
        fr, mr = int(factor_rank), int(ml_rank)
        if in_both:
            header_parts.append(f"多因子#{fr}和ML#{mr}双重确认")
        else:
            header_parts.append(f"因子#{fr}、ML#{mr}")
    if final_score is not None:
        try:
            header_parts.append(f"得分{float(final_score):.1f}")
        except (ValueError, TypeError):
            pass
    if header_parts:
        parts.append("、".join(header_parts))

    # ---- 财务因子关键指标 ----
    kf = reason_data.get("key_factors") or {}

    roe = kf.get("roe_yearly")
    if roe is not None:
        try:
            v = float(roe)
            if v > 15:
                parts.append(f"高 ROE({v:.0f}%)")
            elif v > 8:
                parts.append(f"ROE 良好({v:.0f}%)")
            elif v < 0:
                parts.append(f"亏损 ROE({v:.0f}%)")
        except (ValueError, TypeError):
            pass

    or_yoy = kf.get("or_yoy")
    if or_yoy is not None:
        try:
            v = float(or_yoy)
            if v > 30:
                parts.append(f"营收高增({v:.0f}%)")
            elif v > 10:
                parts.append(f"营收增长({v:.0f}%)")
            elif v < -10:
                parts.append(f"营收下滑({v:.0f}%)")
        except (ValueError, TypeError):
            pass

    dt_eps_yoy = kf.get("dt_eps_yoy")
    if dt_eps_yoy is not None:
        try:
            v = float(dt_eps_yoy)
            if v > 30:
                parts.append(f"扣非EPS高增({v:.0f}%)")
            elif v > 10:
                parts.append(f"盈利改善({v:.0f}%)")
            elif v < -30:
                parts.append(f"盈利下滑({v:.0f}%)")
        except (ValueError, TypeError):
            pass

    debt = kf.get("debt_to_assets")
    if debt is not None:
        try:
            v = float(debt)
            if v > 80:
                parts.append(f"高负债({v:.0f}%)")
            elif v < 30:
                parts.append(f"低负债({v:.0f}%)")
        except (ValueError, TypeError):
            pass

    # ---- 维度得分（三维度分行展示） ----
    dim_scores = reason_data.get("dimension_scores")
    dim_details = reason_data.get("dimension_details")

    if dim_scores and isinstance(dim_scores, dict):
        for dim_name in ["技术面", "基本面", "资金面"]:
            score = dim_scores.get(dim_name)
            if score is None:
                continue
            try:
                s = float(score)
                grade = "优" if s >= 70 else ("良" if s >= 55 else "弱")
            except (ValueError, TypeError):
                continue

            details = dim_details.get(dim_name, {}) if dim_details else {}
            detail_str = _format_dim_detail(dim_name, details)

            if detail_str:
                sub_lines.append(f"  {dim_name}{s:.0f}分({grade})｜{detail_str}")
            else:
                sub_lines.append(f"  {dim_name}{s:.0f}分({grade})")

    # ML 预测收益
    pred_ret = reason_data.get("predicted_return")
    if pred_ret is not None:
        try:
            v = float(pred_ret) * 100
            if v > 5:
                sub_lines.append(f"  ML预测｜看涨，预测20日+{v:.0f}%")
            elif v > 0:
                sub_lines.append(f"  ML预测｜偏多，预测20日+{v:.1f}%")
            elif v > -3:
                sub_lines.append(f"  ML预测｜中性，预测{v:+.1f}%")
            else:
                sub_lines.append(f"  ML预测｜偏空，预测20日{v:.0f}%，注意风险")
        except (ValueError, TypeError):
            pass
    elif ml_rank is not None:
        try:
            mr = int(ml_rank)
            if mr <= 100:
                sub_lines.append(f"  ML预测｜排名靠前(#{mr})，看好")
            elif mr > 2000:
                sub_lines.append(f"  ML预测｜排名靠后(#{mr})，需关注风险")
        except (ValueError, TypeError):
            pass

    # 主力资金流向
    cf = reason_data.get("capital_flow")
    if cf:
        flow_part = _format_capital_flow(cf)
        if flow_part:
            sub_lines.append(f"  资金面｜{flow_part}")

    # ---- 8 维度分析展示 ----
    dims = reason_data.get("eight_dimensions") or {}
    if dims:
        sub_lines.append("  8维度分析:")
        for dim_name, info in dims.items():
            score = info.get("score", 50)
            tier = "优" if score >= 70 else "良" if score >= 50 else "弱"
            dim_items = info.get("items", [])
            item_str = ", ".join(
                f"{it['name']}={it['value']}{'('+it['label']+')' if it.get('label') else ''}"
                for it in dim_items[:2]
            )
            if item_str:
                sub_lines.append(f"    {dim_name} {score}({tier}) | {item_str}")
            else:
                sub_lines.append(f"    {dim_name} {score}({tier})")

    # ---- 交易建议 ----
    ts = reason_data.get("trade_suggestion") or {}
    if ts:
        sub_lines.append(
            f"  建议: 目标 {ts.get('target_price')} (+{ts.get('predicted_return_pct')}%) "
            f"/ 止损 {ts.get('stop_loss')} ({ts.get('stop_loss_pct')*100:.0f}%) "
            f"/ 风险收益比 {ts.get('risk_reward_ratio')}"
        )

    # ---- AI 综合解读 ----
    if dims:
        summary = ai_eight_dimensions_summary(reason_data, name=name)
        if summary:
            sub_lines.append(f"  AI研判: {summary}")

    # ---- 拼装输出 ----
    prefix = f"{name}：" if name else ""
    all_parts = []
    if parts:
        all_parts.append(f"{prefix}{'，'.join(parts)}")
    for sl in sub_lines:
        all_parts.append(sl)

    if all_parts:
        return "\n".join(all_parts)
    return fallback_reason


def _fmt_amount(wan_yuan: float) -> str:
    """万元 → 可读金额（无符号）"""
    if wan_yuan >= 10000:
        return f"{wan_yuan / 10000:.1f}亿"
    elif wan_yuan >= 100:
        return f"{wan_yuan:.0f}万"
    return f"{wan_yuan:.1f}万"


def _fmt_pct(v) -> str:
    """浮点数 → 百分比字符串"""
    try:
        return f"{float(v) * 100:+.1f}%"
    except (ValueError, TypeError):
        return str(v)


def _fmt_raw(v, digits=1) -> str:
    """浮点数 → 原始值字符串"""
    try:
        return f"{float(v):.{digits}f}"
    except (ValueError, TypeError):
        return str(v)


def _format_dim_detail(dim_name: str, details: dict) -> str:
    """
    将维度下的具体因子值翻译成可读指标

    每个维度选取关键指标展示，附加定性标签
    """
    if not details:
        return ""

    items = []

    if dim_name == "技术面":
        mom20 = details.get("20日涨幅")
        rsi = details.get("RSI")
        vol = details.get("10日波动")
        ma5 = details.get("MA5偏离")

        if mom20 is not None:
            try:
                v = float(mom20) * 100
                tag = "强势" if v > 15 else ("偏强" if v > 5 else ("震荡" if v > -5 else "弱势"))
                items.append(f"20日涨{v:+.1f}%({tag})")
            except (ValueError, TypeError):
                pass
        if rsi is not None:
            try:
                v = float(rsi)
                tag = "超买" if v > 70 else ("超卖" if v < 30 else "")
                items.append(f"RSI={v:.0f}" + (f"({tag})" if tag else ""))
            except (ValueError, TypeError):
                pass
        if vol is not None:
            try:
                items.append(f"波动{float(vol)*100:.1f}%")
            except (ValueError, TypeError):
                pass
        if ma5 is not None:
            try:
                items.append(f"MA5偏离{_fmt_pct(ma5)}")
            except (ValueError, TypeError):
                pass

    elif dim_name == "基本面":
        pe = details.get("PE(TTM)")
        pb = details.get("PB")
        tr = details.get("换手率")
        vr = details.get("量比")

        if pe is not None:
            try:
                v = float(pe)
                if v < 0:
                    items.append(f"PE亏损")
                elif v < 15:
                    items.append(f"PE={v:.0f}(低估值)")
                elif v < 30:
                    items.append(f"PE={v:.0f}(合理)")
                else:
                    items.append(f"PE={v:.0f}(偏高)")
            except (ValueError, TypeError):
                pass
        if pb is not None:
            try:
                v = float(pb)
                if v < 1:
                    items.append(f"PB={v:.1f}(破净)")
                elif v < 3:
                    items.append(f"PB={v:.1f}(合理)")
                else:
                    items.append(f"PB={v:.1f}(偏高)")
            except (ValueError, TypeError):
                pass
        if tr is not None:
            try:
                v = float(tr)
                tag = "活跃" if v > 5 else ("清淡" if v < 1 else "正常")
                items.append(f"换手率{v:.1f}%({tag})")
            except (ValueError, TypeError):
                pass
        if vr is not None:
            try:
                items.append(f"量比{float(vr):.2f}")
            except (ValueError, TypeError):
                pass

    elif dim_name == "资金面":
        vs = details.get("量比")
        at5 = details.get("5日均换手")
        ta = details.get("换手加速")
        vpd = details.get("量价背离")

        if vs is not None:
            try:
                v = float(vs)
                tag = "放量" if v > 2 else ("缩量" if v < 0.5 else "")
                items.append(f"量比{v:.1f}" + (f"({tag})" if tag else ""))
            except (ValueError, TypeError):
                pass
        if at5 is not None:
            try:
                items.append(f"5日均换手{float(at5):.1f}%")
            except (ValueError, TypeError):
                pass
        if ta is not None:
            try:
                items.append(f"换手加速{_fmt_pct(ta)}")
            except (ValueError, TypeError):
                pass
        if vpd is not None:
            try:
                items.append(f"量价背离{_fmt_pct(vpd)}")
            except (ValueError, TypeError):
                pass

    return "，".join(items) if items else ""


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


def ai_eight_dimensions_summary(reason_data: dict, name: str = "") -> str:
    """LLM 综合 8 维度 + ML/因子 → 一句话推荐总结（DeepSeek 主，GLM 备）

    所有 LLM 都失败时返回 ""，调用方推送会跳过此行（不阻塞 picks 推送）。
    """
    dims = reason_data.get("eight_dimensions", {})
    if not dims:
        return ""

    # 构建维度摘要
    dim_lines = []
    for dim_name, info in dims.items():
        score = info.get("score", 50)
        items_summary = ", ".join(
            f"{it['name']}={it['value']}" for it in info.get("items", [])[:2]
        )
        dim_lines.append(f"{dim_name} {score}分: {items_summary}")

    industry = reason_data.get("industry", "未知")
    pred_return = reason_data.get("predicted_return", 0)

    prompt = f"""你是A股量化分析师。请基于以下8维度分析+ML预测，给{name}一句话推荐总结（80字内，面向普通投资者）。

行业: {industry}
ML预测20日收益: {pred_return*100:+.1f}%
8维度评分:
{chr(10).join(dim_lines)}

要求:
1. 突出最强信号（哪个维度分最高/最低）
2. 提示一个潜在风险点（哪个维度分较低）
3. 给出"中短线/短线"判断
4. 不要重复数据，用结论性语言"""

    from sentiment.llm_client import chat_simple
    reply = chat_simple(prompt, temperature=0.3, max_tokens=200, timeout=15)
    return reply or ""
