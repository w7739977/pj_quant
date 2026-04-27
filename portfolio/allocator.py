"""
统一组合引擎 — 整合 ETF 轮动 + 小盘多因子 + ML 预测

核心思路（2万初始资金，激进偏好）:
┌─────────────────────────────────────────────┐
│  标准模式: 基于市场情绪的动态 ETF/个股 分配     │
│  激进模式: 100% 个股，3 只集中持仓              │
│                                               │
│  个股来源 = 小盘多因子 ∩ ML 预测（交集加分）     │
└─────────────────────────────────────────────┘

用法:
  python main.py deploy              # 生成今日操作清单
  python main.py deploy --push       # 生成 + 微信推送
  python main.py deploy --simulate   # 模拟执行（更新虚拟持仓）
  python main.py live [--push] [--simulate]  # 激进实盘模式
"""

import json
import logging
import numpy as np
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)


def allocate_capital(sentiment_score: float, total_capital: float,
                     aggressive: bool = False) -> dict:
    """
    根据市场情绪动态分配资金

    Parameters
    ----------
    sentiment_score : float  市场情绪 [-1, 1]
    total_capital : float  总资金
    aggressive : bool  激进模式（跳过 ETF，100% 个股）
    """
    if aggressive:
        return {
            "regime": "激进-全仓个股",
            "sentiment": round(sentiment_score, 3),
            "etf_weight": 0.0,
            "stock_weight": 1.0,
            "etf_capital": 0,
            "stock_capital": round(total_capital),
        }

    if sentiment_score > 0.3:
        etf_w, stock_w = 0.30, 0.70
        regime = "偏多-激进"
    elif sentiment_score > -0.1:
        etf_w, stock_w = 0.50, 0.50
        regime = "中性-均衡"
    else:
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
    from portfolio.trade_utils import is_tradeable

    # Step 1: 多因子打分
    print("  计算多因子得分...")
    factor_df = compute_stock_pool_factors(min_cap=5e8, max_cap=1e10)
    if factor_df.empty:
        return []

    # 过滤不可交易的代码
    factor_df = factor_df[factor_df["code"].apply(is_tradeable)]

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

    # Step 5: 综合排名
    candidates = scored.head(50).copy()
    candidates["ml_rank"] = candidates["code"].map(ml_rank_map).fillna(999).astype(int)
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


def check_holdings(tracker, stop_loss_pct: float = -0.08,
                   take_profit_pct: float = 0.15,
                   max_holding_days: int = 20) -> list:
    """
    检查持仓健康状态，生成卖出信号

    Returns
    -------
    list of dict: [{code, name, shares, price, amount, reason, pnl, pnl_pct}]
    """
    from data.fetcher import fetch_realtime_tencent_batch
    from portfolio.trade_utils import estimate_sell_cost

    holdings = tracker.holdings
    if not holdings:
        return []

    codes = list(holdings.keys())
    sell_actions = []

    # 批量获取实时价格
    try:
        rt_df = fetch_realtime_tencent_batch(codes)
    except Exception as e:
        logger.warning(f"实时行情获取失败: {e}")
        return []

    if rt_df.empty:
        return []

    price_map = {}
    name_map = {}
    for _, row in rt_df.iterrows():
        price_map[row["code"]] = float(row.get("price", 0))
        name_map[row["code"]] = row.get("name", "")

    today = datetime.now()

    for code, info in holdings.items():
        shares = info["shares"]
        avg_cost = info["avg_cost"]
        buy_date = info.get("buy_date", "")

        current_price = price_map.get(code, 0)
        name = name_map.get(code, code)

        if current_price <= 0:
            continue

        pnl_pct = (current_price / avg_cost - 1.0) if avg_cost > 0 else 0
        pnl = (current_price - avg_cost) * shares
        amount = current_price * shares

        reason = ""
        if pnl_pct <= stop_loss_pct:
            reason = "止损"
        elif pnl_pct >= take_profit_pct:
            reason = "止盈"
        elif buy_date:
            try:
                buy_dt = datetime.strptime(buy_date, "%Y-%m-%d")
                days_held = (today - buy_dt).days
                if days_held >= max_holding_days and abs(pnl_pct) < 0.03:
                    reason = f"超时调仓(持有{days_held}日)"
            except ValueError:
                pass

        if reason:
            sell_actions.append({
                "code": code,
                "name": name,
                "shares": shares,
                "price": current_price,
                "amount": round(amount, 2),
                "reason": reason,
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct * 100, 1),
            })

    return sell_actions


def _calc_dimension_scores(candidates: pd.DataFrame, factor_df: pd.DataFrame) -> pd.DataFrame:
    """
    将因子得分拆解为技术面/基本面/资金面三个维度
    每个维度是百分制，便于理解
    """
    # 维度定义和权重
    dimensions = {
        "技术面": {
            "factors": ["mom_5d", "mom_10d", "mom_20d", "mom_60d",
                         "vol_10d", "vol_20d",
                         "ma5_bias", "ma10_bias", "ma20_bias", "rsi_14"],
            "directions": [1, 1, 1, 1, -1, -1, -1, -1, -1, -1],
            "weights": [2, 1, 2, 1, 1, 1, 1, 1, 1, 1],
        },
        "基本面": {
            "factors": ["pe_ttm", "pb", "turnover_rate", "volume_ratio"],
            "directions": [-1, -1, 1, 1],
            "weights": [1.5, 1.5, 1, 1],
        },
        "资金面": {
            "factors": ["avg_turnover_5d", "avg_turnover_20d", "turnover_accel",
                         "volume_surge", "vol_price_diverge"],
            "directions": [1, 1, 1, 1, 1],
            "weights": [1, 1, 1, 1, 1],
        },
    }

    for dim_name, dim_config in dimensions.items():
        dim_score = pd.Series(0.0, index=factor_df.index)
        total_weight = 0.0

        for fac, direction, weight in zip(
            dim_config["factors"], dim_config["directions"], dim_config["weights"]
        ):
            if fac not in factor_df.columns:
                continue
            series = pd.to_numeric(factor_df[fac], errors="coerce")
            valid = series.notna()
            if valid.sum() < 5:
                continue
            rank = series.rank(pct=True, na_option="keep")
            if direction == -1:
                rank = 1 - rank
            dim_score += rank.fillna(0.5) * weight
            total_weight += weight

        if total_weight > 0:
            # 归一化到百分制
            candidates[f"{dim_name}_score"] = round(dim_score / total_weight * 100, 1)
        else:
            candidates[f"{dim_name}_score"] = 50.0

    return candidates


def get_stock_picks_live(stock_capital: float, top_n: int = 3,
                        exclude_codes: list = None) -> list:
    """
    激进模式选股 — 快速路径，跳过耗时的情绪因子计算

    流程:
      1. 计算技术+基本面因子 (~40秒)
      2. ML 模型预测排名
      3. 多因子打分排名
      4. 综合排名取 top N
      5. 实时价格 + 100股整手

    Returns
    -------
    list of dict: [{code, name, shares, price, amount, cost, reason}]
    """
    from data.fetcher import fetch_realtime_tencent_batch
    from portfolio.trade_utils import is_tradeable, calc_shares, estimate_buy_cost
    from factors.calculator import compute_stock_pool_factors

    exclude = set(exclude_codes or [])

    # Step 1: 计算因子（不含情绪，~40秒）
    print("  计算技术+基本面因子...")
    factor_df = compute_stock_pool_factors(min_cap=5e8, max_cap=1e10, skip_sentiment=True)
    if factor_df.empty:
        return []

    # 过滤不可交易的代码
    factor_df = factor_df[factor_df["code"].apply(is_tradeable)]

    # Step 2: 多因子排名
    from strategy.small_cap import SmallCapStrategy
    sc = SmallCapStrategy(top_n=50)
    scored = sc._score_stocks(factor_df)
    scored = scored.sort_values("score", ascending=False)
    scored["factor_rank"] = range(1, len(scored) + 1)

    # Step 3: ML 预测
    ml_rank_map = {}
    pred = pd.DataFrame()
    try:
        from ml.ranker import predict
        print("  ML 模型预测...")
        pred = predict(factor_df)
        if not pred.empty:
            for _, row in pred.iterrows():
                ml_rank_map[row["code"]] = int(row["rank"])
    except Exception as e:
        logger.warning(f"ML 预测失败: {e}")

    # Step 4: 综合排名
    candidates = scored.head(50).copy()
    candidates["ml_rank"] = candidates["code"].map(ml_rank_map).fillna(999).astype(int)
    candidates["in_both"] = (
        (candidates["factor_rank"] <= 20) & (candidates["ml_rank"] <= 20)
    ).astype(int)
    candidates["final_score"] = (
        1.0 / candidates["factor_rank"] * 100
        + 1.0 / candidates["ml_rank"] * 50
        + candidates["in_both"] * 20
    )
    candidates = candidates.sort_values("final_score", ascending=False)

    # Step 4.5: 计算各维度得分拆解（技术面/基本面/资金面）
    candidates = _calc_dimension_scores(candidates, factor_df)

    # 过滤: 可交易 + 未持有
    filtered = candidates[
        ~candidates["code"].isin(exclude)
    ].head(top_n * 3)

    if filtered.empty:
        return []

    # Step 5: 批量获取实时价格
    codes = filtered["code"].tolist()
    try:
        rt_df = fetch_realtime_tencent_batch(codes)
    except Exception as e:
        logger.warning(f"实时行情获取失败: {e}")
        return []

    if rt_df.empty:
        return []

    price_map = {}
    name_map = {}
    for _, row in rt_df.iterrows():
        code = row["code"]
        name = str(row.get("name", ""))
        vol = row.get("volume", 0)
        # 过滤 ST 和停牌
        if "ST" in name or vol == 0:
            continue
        price_map[code] = float(row.get("price", 0))
        name_map[code] = name

    per_stock = stock_capital / top_n

    picks = []
    for _, row in filtered.iterrows():
        if len(picks) >= top_n:
            break

        code = row["code"]
        price = price_map.get(code, 0)
        name = name_map.get(code, code)

        if price <= 0:
            continue

        share_info = calc_shares(per_stock, price)
        if share_info["shares"] < 100:
            continue

        amount = share_info["amount"]
        cost = estimate_buy_cost(amount)

        factor_rank = int(row["factor_rank"])
        ml_rank = int(row["ml_rank"])
        tag = "★双重确认" if row["in_both"] else ""

        # 构建详细理由：排名 + 关键因子指标 + ML预测收益
        reason_parts = [f"因子#{factor_rank} ML#{ml_rank}"]
        if tag:
            reason_parts.append(tag)

        # 提取 top3 关键因子值
        key_factors = []
        for fac in ["mom_20d", "vol_10d", "pe_ttm", "pb", "turnover_rate"]:
            val = row.get(fac)
            if val is not None and not (isinstance(val, float) and (val != val)):
                if fac in ("pe_ttm", "pb"):
                    key_factors.append(f"{fac}:{val:.1f}")
                else:
                    key_factors.append(f"{fac}:{val:+.1%}" if abs(val) < 10 else f"{fac}:{val:.2f}")
        if key_factors:
            reason_parts.append("|".join(key_factors[:3]))

        # ML预测收益
        ml_pred = ml_rank_map.get(code)
        if ml_pred is not None:
            pred_row = pred[pred["code"] == code]
            if not pred_row.empty:
                pred_ret = pred_row.iloc[0].get("predicted_return", 0)
                if pred_ret is not None:
                    reason_parts.append(f"预测20日收益:{pred_ret:+.1%}")

        reason = " ".join(reason_parts)

        picks.append({
            "code": code,
            "name": name,
            "shares": share_info["shares"],
            "price": price,
            "amount": amount,
            "cost": cost,
            "reason": reason,
            "final_score": round(float(row["final_score"]), 2),
            "dimension_scores": {
                "技术面": row.get("技术面_score", None),
                "基本面": row.get("基本面_score", None),
                "资金面": row.get("资金面_score", None),
            },
        })

    # Step 6: 获取买入候选股的主力资金流向（补充展示，不影响选股）
    if picks:
        try:
            from data.fetcher import fetch_capital_flow_batch, _fmt_flow_amount, _fmt_flow_amount_plain
            pick_codes = [p["code"] for p in picks]
            flow_data = fetch_capital_flow_batch(pick_codes)
            for p in picks:
                flow = flow_data.get(p["code"])
                if flow:
                    mf = flow.get("net_mf_amount", 0)
                    elg = flow.get("elg_net", 0)
                    lg = flow.get("lg_net", 0)
                    # 追加资金流向到理由
                    direction = "净流入" if mf >= 0 else "净流出"
                    flow_parts = [f"主力{direction}{_fmt_flow_amount_plain(mf)}"]
                    if abs(elg) >= 1:
                        flow_parts.append(f"超大单{_fmt_flow_amount(elg)}")
                    if abs(lg) >= 1:
                        flow_parts.append(f"大单{_fmt_flow_amount(lg)}")
                    p["reason"] += f" | 资金:{','.join(flow_parts)}"
                    p["capital_flow"] = flow
            if flow_data:
                print(f"  资金流向: {len(flow_data)}/{len(pick_codes)} 只获取成功")
        except Exception as e:
            logger.warning(f"资金流向获取失败(非关键): {e}")

    return picks


def run_live_deploy(push: bool = False, simulate: bool = False) -> dict:
    """
    激进实盘部署 — 生成精确操作清单

    流程:
      1. 加载持仓
      2. 持仓健康检查 (止损/止盈/超时)
      3. simulate模式: 先执行卖出，用实际回笼资金选股
      4. 市场情绪分析
      5. 选股 (实时价格 + 100股整手)
      6. simulate模式: 再执行买入，输出模拟结果
      7. 推送
    """
    from config.settings import (
        INITIAL_CAPITAL, ETF_POOL,
        STOP_LOSS_PCT, TAKE_PROFIT_PCT, MAX_HOLDING_DAYS, NUM_POSITIONS,
    )
    from portfolio.tracker import PortfolioTracker
    from portfolio.trade_utils import (
        format_checklist, format_push_message,
        estimate_sell_cost, estimate_buy_cost,
    )

    print("\n" + "=" * 50)
    print(f"激进实盘部署 - {datetime.now().strftime('%Y-%m-%d')}")
    print("=" * 50)

    # ============ Step 1: 持仓 ============
    tracker = PortfolioTracker()

    # 计算总资产（用持仓成本估算，后面实时价格会更新）
    holdings_value = sum(
        info["shares"] * info.get("avg_cost", 0)
        for info in tracker.holdings.values()
    )
    total_capital = tracker.cash + holdings_value
    print(f"\n总资产(成本): {total_capital:,.0f} 元 | 现金: {tracker.cash:,.0f} 元")

    if tracker.holdings:
        print(f"持仓: {len(tracker.holdings)} 只")
        for code, info in tracker.holdings.items():
            print(f"  {code}: {info['shares']}股 @ {info['avg_cost']:.2f}")

    # ============ Step 2: 持仓健康检查 ============
    print("\n[1/3] 持仓健康检查...")
    sell_actions = check_holdings(
        tracker,
        stop_loss_pct=STOP_LOSS_PCT,
        take_profit_pct=TAKE_PROFIT_PCT,
        max_holding_days=MAX_HOLDING_DAYS,
    )

    if sell_actions:
        print(f"  {len(sell_actions)} 只触发卖出:")
        for a in sell_actions:
            print(f"    {a['name']}({a['code']}) {a['pnl_pct']:+.1f}% {a['reason']}")
    else:
        print("  持仓健康，无卖出信号")

    # ============ Step 2.5: simulate 模式先执行卖出 ============
    if simulate and sell_actions:
        for a in sell_actions:
            cost = estimate_sell_cost(a["amount"])
            tracker.update_after_sell(a["code"], a["price"], cost)
            print(f"  [模拟卖出] {a['name']}({a['code']}) {a['shares']}股"
                  f" @ {a['price']:.2f} 回笼 {a['amount'] - cost:,.0f}元")
        print(f"  卖出后可用资金: {tracker.cash:,.0f} 元")

    # ============ Step 3: 市场情绪 ============
    print("\n[2/3] 市场情绪分析...")
    try:
        from sentiment.analyzer import analyze_market_sentiment
        sent = analyze_market_sentiment()
        sent_score = sent["score"]
        sent_mode = sent["mode"]
        top_news = sent.get("top_news", [])[:3]
        deep = sent.get("deep_analysis")
        print(f"  情绪: {sent_score:+.3f} ({sent_mode})")
    except Exception as e:
        logger.warning(f"情绪分析失败: {e}")
        sent_score, sent_mode, top_news, deep = 0.0, "fallback", [], None

    # ============ Step 4: 选股 ============
    print("\n[3/3] 选股...")

    # 计算可用资金
    if simulate:
        # simulate 模式: 已执行卖出，tracker.cash 是实际可用资金
        available_cash = tracker.cash
    else:
        # 非 simulate: 用预估卖出回笼资金
        available_cash = tracker.cash
        for a in sell_actions:
            sell_cost = estimate_sell_cost(a["amount"])
            available_cash += a["amount"] - sell_cost

    # 已有持仓数量
    current_holdings = len(tracker.holdings)  # simulate 下已剔除卖出的
    slots = max(0, NUM_POSITIONS - current_holdings)

    buy_actions = []
    if slots > 0 and available_cash >= 5000:
        exclude_codes = list(tracker.holdings.keys())
        buy_actions = get_stock_picks_live(
            stock_capital=available_cash,
            top_n=slots,
            exclude_codes=exclude_codes,
        )
        if buy_actions:
            print(f"  选出 {len(buy_actions)} 只:")
            for a in buy_actions:
                print(f"    {a['name']}({a['code']})"
                      f" {a['shares']}股@{a['price']:.2f}"
                      f" = {a['amount']:,.0f}元 {a['reason']}")
        else:
            print("  未选出合适的股票")
    elif slots == 0:
        print(f"  已持有 {current_holdings} 只，仓位已满")
    else:
        print(f"  可用资金不足 ({available_cash:,.0f}元)")

    # ============ simulate 模式执行买入 ============
    if simulate and buy_actions:
        for a in buy_actions:
            tracker.update_after_buy(a["code"], a["shares"], a["price"], a["cost"])
            print(f"  [模拟买入] {a['name']}({a['code']}) {a['shares']}股"
                  f" @ {a['price']:.2f} 花费 {a['amount'] + a['cost']:,.0f}元")

    # ============ 计算实时总资产（操作后） ============
    from data.fetcher import fetch_realtime_tencent_batch
    realtime_total = tracker.cash
    if tracker.holdings:
        holding_codes = list(tracker.holdings.keys())
        try:
            rt_df = fetch_realtime_tencent_batch(holding_codes)
            price_map = {}
            for _, row in rt_df.iterrows():
                price_map[row["code"]] = float(row.get("price", 0))
            for code, info in tracker.holdings.items():
                rt_price = price_map.get(code, info["avg_cost"])
                realtime_total += info["shares"] * rt_price
        except Exception:
            realtime_total = total_capital
    else:
        realtime_total = tracker.cash

    total_pnl = realtime_total - INITIAL_CAPITAL
    total_pnl_pct = total_pnl / INITIAL_CAPITAL * 100

    summary = {
        "total_value": round(realtime_total, 2),
        "cash": round(tracker.cash, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 1),
    }

    # ============ 输出清单 ============
    checklist = format_checklist(sell_actions, buy_actions, summary)
    print(f"\n{checklist}")

    if simulate and (sell_actions or buy_actions):
        print(f"\n--- 模拟后持仓 ---")
        if tracker.holdings:
            for code, info in tracker.holdings.items():
                print(f"  {code}: {info['shares']}股 @ {info['avg_cost']:.4f}")
        else:
            print("  空仓")
        print(f"  现金: {tracker.cash:,.2f} 元")

    # 情绪信息
    if top_news:
        print(f"\n关键新闻:")
        for n in top_news:
            tag = "利多" if n["sentiment"] > 0 else "利空" if n["sentiment"] < 0 else "中性"
            print(f"  [{tag}] {n['title'][:45]}")

    if deep:
        print(f"\nAI 研判: {deep.get('analysis', '')[:100]}")

    # ============ 推送 ============
    if push:
        try:
            from alert.notify import send_to_all
            push_msg = format_push_message(sell_actions, buy_actions, summary)
            title = f"操作清单 ({datetime.now().strftime('%m-%d')})"
            send_to_all(title, push_msg)
            print("\n已推送到微信")
        except Exception as e:
            logger.warning(f"推送失败: {e}")

    return {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "sentiment": sent_score,
        "sell_actions": sell_actions,
        "buy_actions": buy_actions,
        "summary": summary,
    }


def _simulate_execution_live(tracker, sell_actions: list, buy_actions: list):
    """模拟执行操作清单，更新虚拟持仓"""
    from portfolio.trade_utils import estimate_buy_cost, estimate_sell_cost

    # 先卖后买
    for a in sell_actions:
        price = a["price"]
        amount = price * a["shares"]
        cost = estimate_sell_cost(amount)
        tracker.update_after_sell(a["code"], price, cost)
        print(f"  [模拟卖出] {a['code']} {a['shares']}股 @ {price:.2f}")

    for a in buy_actions:
        price = a["price"]
        shares = a["shares"]
        cost = a.get("cost", estimate_buy_cost(price * shares))
        tracker.update_after_buy(a["code"], shares, price, cost)
        print(f"  [模拟买入] {a['code']} {shares}股 @ {price:.2f}")


# ============ 保留原有 deploy 命令 ============

def run_deploy(push: bool = False, simulate: bool = False) -> dict:
    """
    生成今日完整操作清单（标准模式：ETF + 个股）
    """
    from config.settings import INITIAL_CAPITAL, ETF_POOL
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
    if alloc["stock_capital"] >= 5000:
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
            tracker.update_after_buy(
                a["code"],
                shares=1,
                price=a["amount"],
                cost=0,
            )


def _push_deploy_report(actions, alloc, sent_score, top_news, deep):
    """微信推送操作清单"""
    try:
        from alert.notify import send_to_all
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
    send_to_all(title, "\n".join(lines))
    print("操作清单已推送到微信")
