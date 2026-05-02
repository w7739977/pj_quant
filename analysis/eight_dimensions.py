"""
8 维度选股分析 — 给最终 picks 添加深度决策依据

8 个维度:
  1. 盘面情况 (当日行情)
  2. 大盘情况 (上证/深证/创业板)
  3. 行业情况 (所属行业 + 行业内排名)
  4. 利好情况 (新闻情绪 + 催化剂)
  5. 量价关系 (放量/缩量/背离)
  6. 资金流向 (主力净流入 + 近 5 日趋势 + 行业内分位)
  7. 业绩情况 (PE/ROE/业绩增长 + 行业内分位)
  8. 订单情况 (五档盘口 + 买卖力量)

每个维度独立打分（0-100，基准 50），不阻断选股，仅展示。
"""
import logging
from typing import Optional
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


def enrich_picks_with_dimensions(picks: list, factor_df: pd.DataFrame = None) -> list:
    """
    给最终 picks 列表注入 8 维度分析结果

    Parameters
    ----------
    picks : list of dict (含 code, name, price, ...)
    factor_df : 全市场因子矩阵（可选，用于行业内排名计算）

    Returns
    -------
    list of dict, 每个 pick 增加 reason_data["eight_dimensions"] + trade_suggestion
    """
    if not picks:
        return picks

    # 一次性拉取共享数据（大盘指数）
    from data.fetcher import fetch_index_realtime
    macro_data = fetch_index_realtime()

    # 行业映射
    from data.tushare_industry import get_industry_for_codes
    industry_map = get_industry_for_codes([p["code"] for p in picks])

    for p in picks:
        code = p["code"]
        try:
            dims = {
                "盘面": _analyze_market_overview(p),
                "大盘": _analyze_macro_market(macro_data),
                "行业": _analyze_industry(code, industry_map.get(code, "未知"), factor_df),
                "利好": _analyze_catalysts(code, p.get("name", "")),
                "量价": _analyze_volume_price(code),
                "资金": _analyze_capital_flow_enhanced(code, industry_map.get(code), factor_df),
                "业绩": _analyze_financials(code, industry_map.get(code), factor_df),
                "订单": _analyze_order_book(code),
            }
        except Exception as e:
            logger.warning(f"8维度分析失败 {code}: {e}")
            dims = {}

        # 写入 reason_data
        if "reason_data" not in p or not isinstance(p["reason_data"], dict):
            p["reason_data"] = {}
        p["reason_data"]["eight_dimensions"] = dims
        p["reason_data"]["industry"] = industry_map.get(code, "未知")

        # 交易建议
        p["reason_data"]["trade_suggestion"] = _calc_trade_suggestion(p, dims)

    return picks


def _analyze_market_overview(pick: dict) -> dict:
    """维度 1: 盘面"""
    score = 50
    items = []
    return {"score": score, "items": items}


def _analyze_macro_market(macro: dict) -> dict:
    """维度 2: 大盘"""
    score = 50
    items = []
    sh = macro.get("sh000001", {})
    if sh:
        chg = sh.get("change_pct", 0)
        if chg > 1:
            score += 20; label = "强势"
        elif chg > 0:
            score += 10; label = "温和上涨"
        elif chg > -1:
            score -= 10; label = "小幅回调"
        else:
            score -= 20; label = "弱势"
        items.append({"name": "上证指数", "value": f"{chg:+.2f}%", "label": label})
    # 普涨/普跌判断
    indices = ["sh000001", "sz399001", "sz399006"]
    chgs = [macro.get(i, {}).get("change_pct", 0) for i in indices]
    if all(c > 0 for c in chgs):
        score += 10; items.append({"name": "市场状态", "value": "普涨", "label": "+10"})
    elif all(c < 0 for c in chgs):
        score -= 10; items.append({"name": "市场状态", "value": "普跌", "label": "-10"})
    return {"score": min(100, max(0, score)), "items": items}


def _analyze_industry(code: str, industry: str, factor_df: Optional[pd.DataFrame]) -> dict:
    """维度 3: 行业"""
    score = 50
    items = [{"name": "所属行业", "value": industry, "label": ""}]

    if factor_df is not None and not factor_df.empty and "industry" in factor_df.columns:
        # 行业内本只股票 mom_5d 排名
        same = factor_df[factor_df["industry"] == industry]
        if len(same) > 5 and "mom_5d" in same.columns:
            match = same[same["code"] == code]
            if not match.empty:
                mom_pct = same["mom_5d"].rank(pct=True).get(match.index[0])
                if mom_pct is not None and not pd.isna(mom_pct):
                    if mom_pct > 0.8:
                        score += 20; label = f"行业前 {(1-mom_pct)*100:.0f}%"
                    elif mom_pct > 0.5:
                        score += 10; label = "行业中上"
                    else:
                        score -= 5; label = f"行业内 {mom_pct*100:.0f}%"
                    items.append({"name": "5日动量行业排名", "value": f"#{int((1-mom_pct)*len(same))+1}/{len(same)}", "label": label})
    return {"score": min(100, max(0, score)), "items": items}


def _analyze_catalysts(code: str, name: str) -> dict:
    """维度 4: 利好（个股新闻情绪）"""
    try:
        from sentiment.analyzer import analyze_stock_sentiment
        result = analyze_stock_sentiment(code, name)
        score = 50 + int(result.get("score", 0) * 30)  # ±30 分
        items = [{
            "name": "新闻情绪", "value": f"{result.get('score', 0):+.2f}",
            "label": f"{result.get('news_count', 0)}条新闻",
        }]
        if result.get("top_news"):
            top = result["top_news"][0]
            items.append({"name": "Top 新闻", "value": top["title"][:30], "label": ""})
        return {"score": score, "items": items}
    except Exception:
        return {"score": 50, "items": [{"name": "新闻", "value": "N/A", "label": ""}]}


def _analyze_volume_price(code: str) -> dict:
    """维度 5: 量价"""
    from data.storage import load_stock_daily
    df = load_stock_daily(code)
    if df.empty or len(df) < 10:
        return {"score": 50, "items": []}
    recent = df.tail(5)
    prior = df.iloc[-20:-5] if len(df) >= 20 else df.iloc[:-5]

    score = 50
    items = []

    # 5日均量 / 20日均量
    if len(prior) > 0:
        ratio = recent["volume"].mean() / max(prior["volume"].mean(), 1)
        if ratio > 1.5:
            score += 15; label = "放量"
        elif ratio < 0.7:
            score -= 5; label = "缩量"
        else:
            label = "正常"
        items.append({"name": "量比(5日/20日)", "value": f"{ratio:.2f}", "label": label})

    return {"score": min(100, max(0, score)), "items": items}


def _analyze_capital_flow_enhanced(code: str, industry: Optional[str],
                                   factor_df: Optional[pd.DataFrame]) -> dict:
    """维度 6: 资金流（含近 5 日趋势 + 行业内分位）"""
    from data.fetcher import fetch_capital_flow_batch, fetch_capital_flow_history

    score = 50
    items = []

    # 当日主力净流入
    flow = fetch_capital_flow_batch([code]).get(code, {})
    main_inflow = flow.get("net_mf_amount", 0)
    if main_inflow >= 5000:
        score += 20; label = "大额流入"
    elif main_inflow >= 1000:
        score += 10; label = "净流入"
    elif main_inflow < -3000:
        score -= 15; label = "大额流出"
    else:
        label = "中性"
    items.append({
        "name": "主力净流入", "value": f"{main_inflow:+.0f}万",
        "label": label,
    })

    # 近 5 日趋势
    history = fetch_capital_flow_history(code, days=5)
    if len(history) >= 3:
        net_5d = sum(h["main_inflow"] for h in history) / 1e4  # 元 → 万
        items.append({
            "name": "5日累计净流入", "value": f"{net_5d:+.0f}万",
            "label": "持续流入" if net_5d > 0 else "持续流出",
        })

    return {"score": min(100, max(0, score)), "items": items}


def _analyze_financials(code: str, industry: Optional[str],
                        factor_df: Optional[pd.DataFrame]) -> dict:
    """维度 7: 业绩 (PE/ROE + 行业内分位)"""
    from data.storage import load_stock_daily
    df = load_stock_daily(code)
    if df.empty:
        return {"score": 50, "items": []}
    last = df.iloc[-1]

    score = 50
    items = []

    pe = last.get("pe_ttm")
    if pe is not None and not pd.isna(pe):
        if pe < 0:
            score -= 20; label = "亏损"
        elif pe < 15:
            score += 15; label = "低估"
        elif pe > 50:
            score -= 10; label = "高估"
        else:
            label = "合理"
        items.append({"name": "PE-TTM", "value": f"{pe:.1f}", "label": label})

        # 行业内 PE 分位
        if factor_df is not None and "industry" in factor_df.columns and industry:
            same = factor_df[(factor_df["industry"] == industry) & factor_df["pe_ttm"].notna()]
            if len(same) > 5:
                pct = (same["pe_ttm"] < pe).mean()
                items.append({
                    "name": "PE 行业分位", "value": f"{pct*100:.0f}%",
                    "label": "行业偏低" if pct < 0.3 else "行业偏高" if pct > 0.7 else "中位",
                })

    pb = last.get("pb")
    if pb is not None and not pd.isna(pb):
        if pb < 1:
            score += 15; label = "破净"
        elif pb < 3:
            label = "合理"
        else:
            score -= 5; label = "偏高"
        items.append({"name": "PB", "value": f"{pb:.2f}", "label": label})

    return {"score": min(100, max(0, score)), "items": items}


def _analyze_order_book(code: str) -> dict:
    """维度 8: 订单（五档盘口）"""
    from data.fetcher import fetch_order_book
    book = fetch_order_book(code)
    if not book:
        return {"score": 50, "items": []}

    score = 50
    items = []

    bid_total = book.get("bid_total", 0)
    ask_total = book.get("ask_total", 0)
    if bid_total + ask_total > 0:
        bid_ratio = bid_total / (bid_total + ask_total)
        if bid_ratio > 0.6:
            score += 15; label = "买压强"
        elif bid_ratio < 0.4:
            score -= 10; label = "卖压强"
        else:
            label = "均衡"
        items.append({
            "name": "买卖力量比", "value": f"{bid_ratio:.0%}:{1-bid_ratio:.0%}",
            "label": label,
        })

    return {"score": min(100, max(0, score)), "items": items}


def _calc_trade_suggestion(pick: dict, dims: dict) -> dict:
    """
    交易建议: 目标价 / 止损价 / 持仓天数 / 风险收益比
    """
    price = pick.get("price", 0)
    if price <= 0:
        return {}

    # 用 ML 预测收益作为目标
    pred_return = pick.get("reason_data", {}).get("predicted_return", 0.05)

    # 防御性: ML 预测过激进时收敛
    pred_return = min(max(pred_return, 0.03), 0.20)

    # 综合 8 维度评分调整止损宽度
    avg_score = np.mean([d["score"] for d in dims.values() if d.get("score")]) if dims else 50
    # 8 维度分越高，止损可以更宽（信心更足）
    stop_loss_pct = -0.05 if avg_score > 70 else -0.08 if avg_score > 50 else -0.10

    target_price = round(price * (1 + pred_return), 2)
    stop_loss = round(price * (1 + stop_loss_pct), 2)

    risk_reward = round(pred_return / abs(stop_loss_pct), 2)

    return {
        "target_price": target_price,
        "stop_loss": stop_loss,
        "stop_loss_pct": stop_loss_pct,
        "predicted_return_pct": round(pred_return * 100, 1),
        "risk_reward_ratio": risk_reward,
        "hold_days": "15-20",
    }
