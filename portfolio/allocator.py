"""
统一组合引擎 — 整合 ETF 轮动 + 小盘多因子 + ML 预测

核心思路（2万初始资金，激进偏好）:
┌─────────────────────────────────────────────┐
│  资金分配（动态，基于市场情绪）                    │
│                                               │
│  牛市/偏多:  ETF 30% | 个股 70%               │
│  震荡/中性:  ETF 50% | 个股 50%               │
│  熊市/偏空:  ETF 80%(含国债) | 个股 20%        │
│                                               │
│  个股来源 = 小盘多因子 ∩ ML 预测（交集加分）     │
└─────────────────────────────────────────────┘

用法:
  python main.py deploy              # 生成今日操作清单
  python main.py deploy --push       # 生成 + 微信推送
  python main.py deploy --simulate   # 模拟执行（更新虚拟持仓）
"""

import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)


def allocate_capital(sentiment_score: float, total_capital: float) -> dict:
    """
    根据市场情绪动态分配资金

    Parameters
    ----------
    sentiment_score : float  市场情绪 [-1, 1]
    total_capital : float  总资金

    Returns
    -------
    dict: {etf_weight, stock_weight, etf_capital, stock_capital}
    """
    if sentiment_score > 0.3:
        # 偏多: 激进，重仓个股
        etf_w, stock_w = 0.30, 0.70
        regime = "偏多-激进"
    elif sentiment_score > -0.1:
        # 中性: 均衡
        etf_w, stock_w = 0.50, 0.50
        regime = "中性-均衡"
    else:
        # 偏空: 防守，重仓ETF（含国债）
        etf_w, stock_w = 0.80, 0.20
        regime = "偏空-防守"

    return {
        "regime": regime,
        "sentiment": round(sentiment_score, 3),
        "etf_weight": etf_w,
        "stock_weight": stock_w,
        "etf_capital": round(total_capital * etf_w),
        "stock_capital": round(total_capital * stock_w),
    }


def get_etf_signal() -> dict:
    """获取 ETF 轮动信号"""
    try:
        from data.storage import load_daily_data
        from data.fetcher import fetch_etf_daily
        from strategy.etf_rotation import ETFRotationStrategy
        from config.settings import ETF_POOL

        price_data = {}
        for symbol, name in ETF_POOL.items():
            df = load_daily_data(symbol)
            if df.empty:
                try:
                    from datetime import timedelta
                    start = (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
                    df = fetch_etf_daily(symbol, start)
                    if not df.empty:
                        from data.storage import save_daily_data
                        save_daily_data(df, symbol)
                except Exception:
                    pass
            if not df.empty:
                price_data[symbol] = df

        if not price_data:
            return {"signal": "no_data", "target": None}

        strategy = ETFRotationStrategy()
        signals = strategy.generate_signals(price_data)

        if signals.empty:
            return {"signal": "hold", "target": None}

        latest = signals[signals["date"] == signals["date"].max()]
        buys = latest[latest["action"] == "buy"]

        if buys.empty:
            return {"signal": "hold_defense", "target": "511010", "name": "国债ETF"}

        row = buys.iloc[-1]
        sym = row["symbol"]
        return {
            "signal": "rotate",
            "target": sym,
            "name": ETF_POOL.get(sym, sym),
            "momentum": round(float(row.get("momentum", 0)), 4),
        }
    except Exception as e:
        logger.warning(f"ETF 信号获取失败: {e}")
        return {"signal": "error", "error": str(e)}


def get_stock_picks(stock_capital: float, top_n: int = 5) -> list:
    """
    获取个股推荐 — 小盘多因子 ∩ ML 预测，双重确认加分

    Returns
    -------
    list of dict: [{code, score, ml_rank, final_score, amount}]
    """
    from factors.calculator import compute_stock_pool_factors, _batch_sentiment_factors

    # Step 1: 多因子打分
    print("  计算多因子得分...")
    factor_df = compute_stock_pool_factors(min_cap=5e8, max_cap=1e10)
    if factor_df.empty:
        return []

    # Step 2: 情绪因子
    print("  计算情绪因子...")
    factor_df = _batch_sentiment_factors(factor_df)

    # Step 3: 多因子排名
    from strategy.small_cap import SmallCapStrategy
    sc = SmallCapStrategy(top_n=50)
    scored = sc._score_stocks(factor_df)
    scored = scored.sort_values("score", ascending=False)
    scored["factor_rank"] = range(1, len(scored) + 1)

    # Step 4: ML 预测排名
    ml_rank_map = {}
    try:
        from ml.ranker import predict
        print("  ML 模型预测...")
        pred = predict(factor_df)
        if not pred.empty:
            for _, row in pred.iterrows():
                ml_rank_map[row["code"]] = int(row["rank"])
    except Exception as e:
        logger.warning(f"ML 预测失败: {e}")

    # Step 5: 综合排名 = 多因子排名 + ML 排名，交集加分
    candidates = scored.head(50).copy()
    candidates["ml_rank"] = candidates["code"].map(ml_rank_map).fillna(999).astype(int)

    # 综合得分: factor_rank 越小越好 + ml_rank 越小越好
    # 同时出现在两个 top 20 的加分
    candidates["in_both"] = (
        (candidates["factor_rank"] <= 20) & (candidates["ml_rank"] <= 20)
    ).astype(int)
    candidates["final_score"] = (
        1.0 / candidates["factor_rank"] * 100
        + 1.0 / candidates["ml_rank"] * 50
        + candidates["in_both"] * 20
    )

    candidates = candidates.sort_values("final_score", ascending=False)
    top = candidates.head(top_n)

    per_stock = round(stock_capital / top_n, 0)

    picks = []
    for _, row in top.iterrows():
        picks.append({
            "code": row["code"],
            "factor_rank": int(row["factor_rank"]),
            "ml_rank": int(row["ml_rank"]),
            "in_both": bool(row["in_both"]),
            "final_score": round(float(row["final_score"]), 2),
            "amount": per_stock,
        })

    return picks


def run_deploy(push: bool = False, simulate: bool = False) -> dict:
    """
    生成今日完整操作清单

    Returns
    -------
    dict: 完整部署报告
    """
    from config.settings import INITIAL_CAPITAL, PUSHPLUS_TOKEN, ETF_POOL
    from portfolio.tracker import PortfolioTracker

    print("\n" + "=" * 60)
    print(f"统一部署引擎 - {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 60)

    tracker = PortfolioTracker()
    total_capital = tracker.cash
    for sym, info in tracker.holdings.items():
        total_capital += info["shares"] * info.get("avg_cost", 0)

    print(f"\n当前总资产: {total_capital:,.0f} 元")

    # ============ Step 1: 市场情绪 ============
    print("\n[1/3] 市场情绪分析...")
    try:
        from sentiment.analyzer import analyze_market_sentiment
        sent = analyze_market_sentiment()
        sent_score = sent["score"]
        sent_mode = sent["mode"]
        top_news = sent.get("top_news", [])[:3]
        deep = sent.get("deep_analysis")
        print(f"  情绪: {sent_score:+.3f} ({sent_mode})")
        if deep:
            print(f"  主线: {deep.get('theme', 'N/A')}")
            print(f"  建议: {deep.get('action', 'N/A')}")
    except Exception as e:
        logger.warning(f"情绪分析失败，使用中性: {e}")
        sent_score, sent_mode, top_news, deep = 0.0, "fallback", [], None

    # ============ Step 2: 资金分配 ============
    print("\n[2/3] 资金分配...")
    alloc = allocate_capital(sent_score, total_capital)
    print(f"  市场状态: {alloc['regime']}")
    print(f"  ETF 仓位: {alloc['etf_weight']:.0%} = {alloc['etf_capital']:,.0f} 元")
    print(f"  个股仓位: {alloc['stock_weight']:.0%} = {alloc['stock_capital']:,.0f} 元")

    # ============ Step 3: 信号生成 ============
    print("\n[3/3] 信号生成...")

    # 3a. ETF 信号
    print("  ETF 轮动...")
    etf_sig = get_etf_signal()

    # 3b. 个股推荐
    print("  个股精选...")
    stock_picks = []
    if alloc["stock_capital"] >= 5000:  # 个股资金 > 5000 才选
        stock_picks = get_stock_picks(alloc["stock_capital"], top_n=5)

    # ============ 生成操作清单 ============
    actions = []

    # ETF 操作
    if etf_sig["signal"] in ("rotate", "hold_defense"):
        target = etf_sig["target"]
        actions.append({
            "type": "ETF",
            "action": "买入",
            "code": target,
            "name": etf_sig.get("name", ETF_POOL.get(target, target)),
            "amount": alloc["etf_capital"],
            "reason": f"动量轮动 (mom={etf_sig.get('momentum', 0):+.2%})",
        })
    elif etf_sig["signal"] == "hold":
        actions.append({
            "type": "ETF",
            "action": "持有",
            "code": "当前持仓",
            "name": "不动",
            "amount": alloc["etf_capital"],
            "reason": "轮动信号未触发",
        })

    # 个股操作
    for pick in stock_picks:
        tag = "★双重确认" if pick["in_both"] else ""
        actions.append({
            "type": "个股",
            "action": "买入",
            "code": pick["code"],
            "name": "",
            "amount": pick["amount"],
            "reason": f"因子#{pick['factor_rank']} ML#{pick['ml_rank']} {tag}",
        })

    # ============ 输出报告 ============
    print(f"\n{'='*60}")
    print(f"今日操作清单")
    print(f"{'='*60}")
    print(f"市场: {alloc['regime']} | 情绪: {sent_score:+.3f}")
    print(f"资金: ETF {alloc['etf_capital']:,.0f} | 个股 {alloc['stock_capital']:,.0f}")
    print(f"{'─'*60}")

    for a in actions:
        print(f"  [{a['type']}] {a['action']} {a['code']} {a['name']}")
        print(f"    金额: {a['amount']:,.0f} 元 | {a['reason']}")

    if top_news:
        print(f"{'─'*60}")
        print("关键新闻:")
        for n in top_news:
            tag = "利多" if n["sentiment"] > 0 else "利空" if n["sentiment"] < 0 else "中性"
            print(f"  [{tag}] {n['title'][:45]}")

    if deep:
        print(f"{'─'*60}")
        print(f"AI 研判: {deep.get('analysis', '')[:100]}")
        print(f"风险: {', '.join(deep.get('risks', []))}")

    print(f"{'='*60}")

    # ============ 模拟执行 ============
    if simulate:
        _simulate_execution(tracker, actions, alloc)
        print("\n虚拟持仓已更新")

    # ============ 推送 ============
    if push:
        _push_deploy_report(actions, alloc, sent_score, top_news, deep)

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "sentiment": sent_score,
        "allocation": alloc,
        "actions": actions,
        "stock_picks": stock_picks,
    }


def _simulate_execution(tracker, actions: list, alloc: dict):
    """模拟执行操作清单，更新虚拟持仓"""
    for a in actions:
        if a["action"] == "买入" and a["code"] != "当前持仓":
            # 简化: 按金额等价买入，不计精确股数
            tracker.update_after_buy(
                a["code"],
                shares=1,  # 占位
                price=a["amount"],  # 用金额代替价格
                cost=0,
            )


def _push_deploy_report(actions, alloc, sent_score, top_news, deep):
    """微信推送操作清单"""
    try:
        from alert.notify import send_message
        from config.settings import PUSHPLUS_TOKEN
    except ImportError:
        print("推送模块不可用")
        return

    lines = [
        f"**市场**: {alloc['regime']} | 情绪 {sent_score:+.3f}",
        f"**资金**: ETF {alloc['etf_capital']:,.0f} | 个股 {alloc['stock_capital']:,.0f}",
        "",
        "**操作:**",
    ]
    for a in actions:
        lines.append(f"- [{a['type']}] {a['action']} {a['code']} ({a['amount']:,.0f})")

    if top_news:
        lines.append("")
        lines.append("**关键新闻:**")
        for n in top_news[:3]:
            tag = "利多" if n["sentiment"] > 0 else "利空"
            lines.append(f"  [{tag}] {n['title'][:30]}")

    title = f"操作清单 ({alloc['regime']})"
    send_message(title, "\n".join(lines), PUSHPLUS_TOKEN)
    print("操作清单已推送到微信")
