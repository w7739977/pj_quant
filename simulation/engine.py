"""
模拟交易引擎 — 常驻进程

流程:
  09:10  on_pre_open()    加载昨日计划，生成今日订单
  09:33~ on_bar()         每3分钟轮询：行情→撮合→止损检查
  15:00  on_after_close() 收盘结算+生成明日计划+推送

用法:
  python main.py sim --start      # 常驻进程
  python main.py sim --run-once   # 单次执行（测试）
"""

import json
import logging
import signal
import sys
import os
import time
from datetime import datetime, timedelta

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

# A股交易时间段
MARKET_OPEN = "09:30"
MARKET_CLOSE = "15:00"
BAR_INTERVAL_SECONDS = 180  # 3分钟轮询


def is_trading_day() -> bool:
    """判断今天是否 A 股交易日（排除周末 + 法定节假日 + 调休补班的周六）

    注意: chinese_calendar.is_workday 在调休补班的周六返回 True（视为工作日），
    但 A 股交易所**不**在周六/周日开盘，无论是否补班。所以这里强制叠加 weekday<5。
    """
    today = datetime.now().date()
    if today.weekday() >= 5:
        return False
    try:
        import chinese_calendar
        return chinese_calendar.is_workday(today)
    except Exception:
        return True  # 已通过 weekday 检查为工作日，库不可用时默认开盘


def is_market_hours() -> bool:
    """判断当前是否交易时间（工作日 + 9:30-15:00）"""
    if not is_trading_day():
        return False
    now = datetime.now().strftime("%H:%M")
    return MARKET_OPEN <= now < MARKET_CLOSE


def _calc_single_stock_dims(factors: dict) -> dict:
    """
    给单只股票计算三维度得分（技术面/基本面/资金面）
    返回每个维度的: 总分、各因子原始值和加减分明细
    """
    import math

    def _valid(v):
        return v is not None and not (isinstance(v, float) and math.isnan(v))

    def _fmt(v, pct=False):
        if v is None: return "N/A"
        try:
            f = float(v)
            if pct: return f"{f*100:+.1f}%"
            return f"{f:.1f}"
        except (ValueError, TypeError):
            return "N/A"

    # ---- 技术面: 动量 + RSI + MA偏离 ----
    tech_items = []
    tech_score = 50

    mom = factors.get("mom_20d")
    if _valid(mom):
        delta, label = 0, "震荡"
        if mom > 0.15:
            delta, label = 20, "强势"
        elif mom > 0.05:
            delta, label = 10, "偏强"
        elif mom < -0.10:
            delta, label = -20, "弱势"
        tech_score += delta
        tech_items.append({"name": "20日涨幅", "value": _fmt(mom, True), "delta": delta, "label": label})

    rsi = factors.get("rsi_14")
    if _valid(rsi):
        delta, label = 0, "正常"
        if rsi > 70:
            delta, label = -5, "超买"
        elif rsi < 30:
            delta, label = 10, "超卖反弹"
        tech_score += delta
        tech_items.append({"name": "RSI", "value": f"{float(rsi):.0f}", "delta": delta, "label": label})

    ma5 = factors.get("ma5_bias")
    if _valid(ma5):
        tech_items.append({"name": "MA5偏离", "value": _fmt(ma5, True), "delta": 0, "label": ""})

    tech_score = max(0, min(100, tech_score))

    # ---- 基本面: PE + PB ----
    fund_items = []
    fund_score = 50

    pe = factors.get("pe_ttm")
    if _valid(pe):
        delta, label = 0, "合理"
        if pe < 0:
            delta, label = -20, "亏损"
        elif pe < 15:
            delta, label = 20, "低估值"
        elif pe > 50:
            delta, label = -10, "高估值"
        fund_score += delta
        fund_items.append({"name": "PE", "value": f"{float(pe):.1f}", "delta": delta, "label": label})

    pb = factors.get("pb")
    if _valid(pb):
        delta, label = 0, ""
        if pb < 1:
            delta, label = 15, "破净"
        elif pb > 5:
            delta, label = -5, "估值偏高"
        fund_score += delta
        fund_items.append({"name": "PB", "value": f"{float(pb):.1f}", "delta": delta, "label": label})

    fund_score = max(0, min(100, fund_score))

    # ---- 资金面: 换手率 + 量比 + 放量 ----
    cap_items = []
    cap_score = 50

    tr = factors.get("turnover_rate")
    if _valid(tr):
        delta, label = 0, "正常"
        if tr > 10:
            delta, label = 15, "活跃"
        elif tr > 3:
            delta, label = 5, "正常"
        else:
            delta, label = -5, "清淡"
        cap_score += delta
        cap_items.append({"name": "换手率", "value": f"{float(tr):.1f}%", "delta": delta, "label": label})

    vs = factors.get("volume_surge")
    if _valid(vs):
        delta, label = 0, ""
        if vs > 2:
            delta, label = 10, "放量"
        cap_score += delta
        cap_items.append({"name": "量比", "value": f"{float(vs):.1f}", "delta": delta, "label": label})

    vr = factors.get("volume_ratio")
    if _valid(vr):
        cap_items.append({"name": "成交比", "value": f"{float(vr):.2f}", "delta": 0, "label": ""})

    cap_score = max(0, min(100, cap_score))

    return {
        "技术面": {"score": tech_score, "items": tech_items},
        "基本面": {"score": fund_score, "items": fund_items},
        "资金面": {"score": cap_score, "items": cap_items},
    }



class SimEngine:
    """模拟交易引擎"""

    def __init__(self):
        from simulation.matcher import Matcher, Order
        from simulation.trade_log import (
            load_sim_portfolio, save_sim_portfolio,
            save_order, update_order_status, save_trade,
            save_snapshot, get_latest_snapshot,
        )
        from config.settings import (
            INITIAL_CAPITAL, STOP_LOSS_PCT, TAKE_PROFIT_PCT,
            MAX_HOLDING_DAYS, NUM_POSITIONS,
        )

        self.Matcher = Matcher
        self.Order = Order
        self.matcher = Matcher()
        self.load_portfolio = load_sim_portfolio
        self.save_portfolio = save_sim_portfolio
        self.save_order = save_order
        self.update_order_status = update_order_status
        self.save_trade = save_trade
        self.save_snapshot = save_snapshot
        self.get_latest_snapshot = get_latest_snapshot

        self.initial_capital = INITIAL_CAPITAL
        self.stop_loss_pct = STOP_LOSS_PCT
        self.take_profit_pct = TAKE_PROFIT_PCT
        self.max_holding_days = MAX_HOLDING_DAYS
        self.max_positions = NUM_POSITIONS

        self.portfolio = load_sim_portfolio()
        self.orders_pending = []       # 待执行订单
        self.daily_plan = {}           # 今日操作计划
        self._running = False
        self._plan_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "sim_daily_plan.json"
        )

    def reset(self):
        """重置模拟盘"""
        self.portfolio = {
            "cash": self.initial_capital,
            "holdings": {},
        }
        self.save_portfolio(self.portfolio)
        self.orders_pending = []
        self.daily_plan = []
        if os.path.exists(self._plan_file):
            os.remove(self._plan_file)
        # 清空 SQLite 数据
        from simulation.trade_log import _get_conn
        conn = _get_conn()
        conn.executescript("DELETE FROM sim_orders; DELETE FROM sim_trades; DELETE FROM sim_snapshots;")
        conn.close()
        print("模拟盘已重置")

    def status(self) -> str:
        """当前状态概览"""
        from simulation.matcher import fetch_quotes_batch
        portfolio = self.portfolio
        cash = portfolio.get("cash", 0)
        holdings = portfolio.get("holdings", {})
        lines = [
            "=" * 50,
            f"模拟盘状态 ({datetime.now().strftime('%Y-%m-%d %H:%M')})",
            "=" * 50,
            f"初始资金: {self.initial_capital:,.0f}元",
            f"可用现金: {cash:,.0f}元",
        ]

        total_value = cash
        if holdings:
            codes = list(holdings.keys())
            quotes = fetch_quotes_batch(codes)
            lines.append(f"持仓: {len(holdings)}只")
            for code, info in holdings.items():
                shares = info["shares"]
                avg_cost = info["avg_cost"]
                q = quotes.get(code, {})
                price = q.get("price", avg_cost)
                name = q.get("name", "")
                mv = price * shares
                pnl = (price - avg_cost) * shares
                pnl_pct = (price / avg_cost - 1) * 100 if avg_cost > 0 else 0
                total_value += mv
                lines.append(
                    f"  {code} {name}: {shares}股 @ {avg_cost:.3f}"
                    f" → {price:.3f}"
                    f" | 市值{mv:,.0f} | 盈亏{pnl:+,.0f}({pnl_pct:+.1f}%)"
                )
        else:
            lines.append("持仓: 空仓")

        total_ret = (total_value / self.initial_capital - 1) * 100
        lines.extend([
            "-" * 50,
            f"总资产: {total_value:,.0f}元 | 累计收益: {total_ret:+.2f}%",
            "=" * 50,
        ])
        return "\n".join(lines)

    # ============ 盘前准备 ============

    def on_pre_open(self):
        """
        09:10 盘前准备
        加载昨日生成的今日计划，转化为待执行订单
        """
        logger.info("盘前准备: 加载今日操作计划")
        print(f"\n[盘前] {datetime.now().strftime('%H:%M')} 加载操作计划...")

        # 加载计划
        if os.path.exists(self._plan_file):
            with open(self._plan_file, "r") as f:
                self.daily_plan = json.load(f)
        else:
            self.daily_plan = {}

        self.orders_pending = []

        # 计划中的卖出订单
        for sell in self.daily_plan.get("sells", []):
            code = sell["code"]
            shares = sell["shares"]
            reason = sell.get("reason", "计划卖出")
            order = self.Order(code, "sell", shares, reason=reason)
            self.orders_pending.append(order)
            self.save_order(order)
            print(f"  [卖出计划] {code} {shares}股 {reason}")

        # 计划中的买入订单（限制数量不超过可用仓位）
        current_holdings = len(self.portfolio.get("holdings", {}))
        max_buys = max(0, self.max_positions - current_holdings)
        for buy in self.daily_plan.get("buys", []):
            if max_buys <= 0:
                print(f"  [跳过买入] 仓位已满，跳过 {buy.get('code', '')}")
                break
            code = buy["code"]
            capital = buy.get("amount", 0)
            price = buy.get("price", 0)
            if price <= 0:
                continue
            from portfolio.trade_utils import calc_shares
            share_info = calc_shares(capital, price)
            if share_info["shares"] < 100:
                continue
            reason = buy.get("reason", "计划买入")
            order = self.Order(code, "buy", share_info["shares"],
                               reason=reason)
            self.orders_pending.append(order)
            self.save_order(order)
            print(f"  [买入计划] {code} {share_info['shares']}股@{price:.2f} {reason}")
            max_buys -= 1

        if not self.orders_pending:
            print("  今日无计划订单")

        # 刷新持仓（从文件重新加载）
        self.portfolio = self.load_portfolio()

    # ============ 盘中轮询 ============

    def on_bar(self):
        """
        每3分钟执行一次
        1. 拉取持仓+待执行订单的行情
        2. 撮合待执行订单
        3. 实时止损/止盈检查
        """
        # 收集需要行情的代码
        codes = set(self.portfolio.get("holdings", {}).keys())
        for o in self.orders_pending:
            codes.add(o.symbol)
        if not codes:
            return

        from simulation.matcher import fetch_quotes_batch
        quotes = fetch_quotes_batch(list(codes))

        # 1. 撮合待执行订单
        filled_orders = []
        remaining = []
        for order in self.orders_pending:
            q = quotes.get(order.symbol, {})
            if not q or q.get("price", 0) <= 0:
                remaining.append(order)
                continue

            # T+1 检查
            if order.side == "sell":
                holding = self.portfolio["holdings"].get(order.symbol, {})
                buy_date = holding.get("buy_date", "")
                if not self.matcher.can_sell_today(buy_date):
                    remaining.append(order)
                    continue

            self.matcher.match(order, q)

            if order.status == "filled":
                self._execute_order(order, q)
                filled_orders.append(order)
            elif order.status == "rejected":
                logger.info(f"订单拒绝: {order.symbol} {order.reason}")
                print(f"  [拒绝] {order.symbol} {order.reason}")
                self.update_order_status(order)
            else:
                remaining.append(order)

        self.orders_pending = remaining

        # 2. 实时止损检查（重新读取 holdings，因为撮合可能已修改）
        holdings = self.portfolio.get("holdings", {})
        for code, info in list(holdings.items()):
            q = quotes.get(code, {})
            if not q or q.get("price", 0) <= 0:
                continue

            # 跳过刚在订单撮合中已卖出的
            if code not in self.portfolio.get("holdings", {}):
                continue

            stop_order = self.matcher.check_stop_loss(
                code, info["shares"], info["avg_cost"],
                info.get("buy_date", ""), q,
                self.stop_loss_pct, self.take_profit_pct,
                self.max_holding_days,
            )
            if stop_order:
                # T+1 检查
                if self.matcher.can_sell_today(info.get("buy_date", "")):
                    self.matcher.match(stop_order, q)
                    if stop_order.status == "filled":
                        self._execute_order(stop_order, q)
                        print(f"  [止损/止盈触发] {code} {stop_order.reason}"
                              f" @ {stop_order.filled_price:.2f}")

    def _execute_order(self, order, quote: dict):
        """执行已成交的订单，更新持仓"""
        name = quote.get("name", order.symbol)
        price = order.filled_price
        shares = order.filled_shares
        fee = order.fee
        amount = price * shares

        if order.side == "buy":
            # 检查资金
            if amount + fee > self.portfolio["cash"]:
                order.status = "rejected"
                order.reason += " [资金不足]"
                self.update_order_status(order)
                return

            self.portfolio["cash"] -= (amount + fee)
            if order.symbol in self.portfolio["holdings"]:
                old = self.portfolio["holdings"][order.symbol]
                total_shares = old["shares"] + shares
                total_cost = old["avg_cost"] * old["shares"] + price * shares
                self.portfolio["holdings"][order.symbol] = {
                    "shares": total_shares,
                    "avg_cost": round(total_cost / total_shares, 4),
                    "buy_date": old.get("buy_date",
                                        datetime.now().strftime("%Y-%m-%d")),
                }
            else:
                self.portfolio["holdings"][order.symbol] = {
                    "shares": shares,
                    "avg_cost": price,
                    "buy_date": datetime.now().strftime("%Y-%m-%d"),
                }

            self.save_trade(
                order.symbol, name, "buy", shares, price, amount, fee,
                reason=order.reason, order_id=order.order_id,
            )
            print(f"  [成交买入] {name}({order.symbol})"
                  f" {shares}股@{price:.2f} = {amount:,.0f}元"
                  f" 手续费{fee:.2f}")

        elif order.side == "sell":
            if order.symbol not in self.portfolio["holdings"]:
                order.status = "rejected"
                order.reason += " [无持仓]"
                self.update_order_status(order)
                return

            old = self.portfolio["holdings"][order.symbol]
            cost_basis = old["avg_cost"] * shares
            profit = amount - cost_basis - fee

            self.portfolio["cash"] += (amount - fee)
            del self.portfolio["holdings"][order.symbol]

            self.save_trade(
                order.symbol, name, "sell", shares, price, amount, fee,
                profit=round(profit, 2),
                reason=order.reason, order_id=order.order_id,
            )
            print(f"  [成交卖出] {name}({order.symbol})"
                  f" {shares}股@{price:.2f} = {amount:,.0f}元"
                  f" 盈亏{profit:+,.0f}元")

        self.save_portfolio(self.portfolio)
        self.update_order_status(order)

    # ============ 收盘结算 ============

    def on_after_close(self, push: bool = False):
        """
        15:00 收盘结算
        1. 取消所有未成交订单
        2. 保存每日快照
        3. 生成明日计划
        4. 推送当日报告
        """
        print(f"\n[收盘] {datetime.now().strftime('%H:%M')} 开始结算...")

        # 1. 取消未成交订单
        for order in self.orders_pending:
            order.status = "cancelled"
            self.update_order_status(order)
            print(f"  [取消] {order.symbol} {order.side} {order.shares}股")
        self.orders_pending = []

        # 2. 保存每日快照
        self._save_daily_snapshot()

        # 3. 生成明日计划
        self._generate_next_plan()

        # 4. 推送报告
        if push:
            self._push_daily_report()

        # 输出日报
        from simulation.report import daily_report
        print(daily_report())

    def _evaluate_rotation(self, remaining_holdings: dict, quotes: dict) -> tuple:
        """
        调仓评估: 对比持仓因子得分与候选池，找出值得换仓的弱股

        Returns
        -------
        (rotation_sells, holding_scores)
        """
        from factors.calculator import compute_all_factors
        from strategy.small_cap import SmallCapStrategy

        # 1. 给持仓算因子
        holding_factors = {}
        for code in remaining_holdings:
            f = compute_all_factors(code)
            if f:
                holding_factors[code] = f

        if not holding_factors:
            return [], {}

        hf_df = pd.DataFrame(holding_factors.values())
        sc = SmallCapStrategy(top_n=50)
        scored = sc._score_stocks(hf_df)
        scored = scored.sort_values("score", ascending=False)
        scored["factor_rank_local"] = range(1, len(scored) + 1)

        # 2. ML预测持仓
        ml_ranks = {}
        try:
            from ml.ranker import predict
            pred = predict(hf_df)
            if not pred.empty:
                for _, row in pred.iterrows():
                    ml_ranks[row["code"]] = int(row["rank"])
        except Exception:
            pass

        # 3. 计算持仓得分
        holding_scores = {}
        for _, row in scored.iterrows():
            code = row["code"]
            fr = int(row["factor_rank_local"])
            mr = ml_ranks.get(code, 999)
            in_both = 1 if fr <= 20 and mr <= 20 else 0
            final_score = (1.0 / fr * 100 + 1.0 / mr * 50 + in_both * 20)
            holding_scores[code] = {
                "factor_rank": fr,
                "ml_rank": mr,
                "final_score": final_score,
            }

        # 4. 获取候选池 top3 的分数作为标杆
        # 直接复用 get_stock_picks_live 的内部排名，但只取分数参考
        # 用持仓中得分最高的作为"阈值"，低于此值50%+且浮亏则调仓
        if not holding_scores:
            return [], {}

        scores = [v["final_score"] for v in holding_scores.values()]
        max_hold_score = max(scores) if scores else 0

        rotation_sells = []
        for code, info in remaining_holdings.items():
            if code not in holding_scores:
                continue
            hs = holding_scores[code]
            q = quotes.get(code, {})
            price = q.get("price", info["avg_cost"])
            if price <= 0 or info["avg_cost"] <= 0:
                continue
            pnl_pct = price / info["avg_cost"] - 1.0
            pnl_amount = (price - info["avg_cost"]) * info["shares"]
            days_held = 0
            if info.get("buy_date"):
                try:
                    buy_dt = datetime.strptime(info["buy_date"], "%Y-%m-%d")
                    days_held = (datetime.now() - buy_dt).days
                except ValueError:
                    pass

            # 调仓条件:
            # - 持仓得分远低于组合内最高分(差距>50%)
            # - 且浮亏或微盈(<3%)
            # - 持有超过5天(避免刚买就换)
            score_ratio = hs["final_score"] / max_hold_score if max_hold_score > 0 else 1
            if (score_ratio < 0.5
                    and pnl_pct < 0.03
                    and days_held >= 5
                    and hs["final_score"] < 30):
                name = q.get("name", code)
                rotation_sells.append({
                    "code": code,
                    "name": name,
                    "shares": info["shares"],
                    "price": price,
                    "reason": (
                        f"调仓换股(得分{hs['final_score']:.0f}远低于最优{max_hold_score:.0f},"
                        f" 收益{pnl_pct:+.1%}, 持有{days_held}日)"
                    ),
                })

        # 按得分从低到高排序，优先换最差的
        rotation_sells.sort(key=lambda x: holding_scores.get(x["code"], {}).get("final_score", 999))
        return rotation_sells, holding_scores

    def _analyze_holdings(self, holdings: dict, quotes: dict) -> list:
        """
        分析每只持仓的因子状态，供报告展示

        Returns
        -------
        list of dict: [{code, name, pnl_pct, pnl_amount, days_held, factors}]
        """
        from factors.calculator import compute_all_factors

        results = []
        for code, info in holdings.items():
            q = quotes.get(code, {})
            price = q.get("price", info["avg_cost"])
            name = q.get("name", code)
            pnl_pct = (price / info["avg_cost"] - 1) if info["avg_cost"] > 0 else 0
            pnl_amount = (price - info["avg_cost"]) * info["shares"] if info["avg_cost"] > 0 else 0
            days_held = 0
            if info.get("buy_date"):
                try:
                    buy_dt = datetime.strptime(info["buy_date"], "%Y-%m-%d")
                    days_held = (datetime.now() - buy_dt).days
                except ValueError:
                    pass

            # 计算因子
            factors = compute_all_factors(code)

            # 计算维度得分
            dim_scores = _calc_single_stock_dims(factors) if factors else {}

            # 个股情绪分析（新闻/利好利空）
            sentiment = {}
            try:
                from sentiment.analyzer import analyze_stock_sentiment
                sentiment = analyze_stock_sentiment(code, name)
            except Exception as e:
                logger.debug(f"个股情绪分析失败 {code}: {e}")

            results.append({
                "code": code,
                "name": name,
                "price": round(price, 2),
                "avg_cost": info["avg_cost"],
                "pnl_pct": round(pnl_pct * 100, 1),
                "pnl_amount": round(pnl_amount, 0),
                "days_held": days_held,
                "factors": {
                    "mom_20d": factors.get("mom_20d"),
                    "vol_10d": factors.get("vol_10d"),
                    "rsi_14": factors.get("rsi_14"),
                    "pe_ttm": factors.get("pe_ttm"),
                    "pb": factors.get("pb"),
                    "turnover_rate": factors.get("turnover_rate"),
                },
                "dimension_scores": dim_scores,
                "sentiment": sentiment,
            })
        return results

    def _save_daily_snapshot(self):
        """保存每日快照"""
        from simulation.matcher import fetch_quotes_batch
        portfolio = self.portfolio
        cash = portfolio.get("cash", 0)
        holdings = portfolio.get("holdings", {})

        market_value = 0
        if holdings:
            codes = list(holdings.keys())
            quotes = fetch_quotes_batch(codes)
            for code, info in holdings.items():
                q = quotes.get(code, {})
                price = q.get("price", info["avg_cost"])
                market_value += price * info["shares"]

        total_value = cash + market_value

        # 日收益率
        prev = self.get_latest_snapshot()
        prev_total = prev["total_value"] if prev else self.initial_capital
        daily_return = (total_value / prev_total - 1) if prev_total > 0 else 0
        total_return = (total_value / self.initial_capital - 1)

        # 今日交易
        from simulation.trade_log import get_today_trades
        trades = get_today_trades()

        # 持仓快照
        self.save_snapshot(
            cash=round(cash, 2),
            market_value=round(market_value, 2),
            total_value=round(total_value, 2),
            daily_return=round(daily_return, 4),
            total_return=round(total_return, 4),
            positions=holdings,
            trades=[{k: v for k, v in t.items() if k != "id"} for t in trades],
        )
        print(f"  快照已保存: 总资产{total_value:,.0f}元"
              f" 日收益{daily_return:+.2%}")

    def _generate_next_plan(self):
        """
        生成明日操作计划
        1. 检查持仓是否需要卖出（止损/止盈/超时）
        2. 调仓评估: 对比持仓与候选池
        3. 选股生成买入计划
        4. 保存计划文件
        """
        print("\n  生成明日操作计划...")

        # 计算下一个交易日（排除周末和中国法定节假日）
        try:
            import chinese_calendar
            next_day = datetime.now().date() + timedelta(days=1)
            while not chinese_calendar.is_workday(next_day):
                next_day += timedelta(days=1)
        except Exception:
            next_day = datetime.now() + timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
        plan = {"date": next_day.strftime("%Y-%m-%d"),
                "sells": [], "buys": []}

        portfolio = self.portfolio
        holdings = portfolio.get("holdings", {})
        cash = portfolio.get("cash", 0)

        # 1. 卖出计划: 用当前价检查止损/止盈/超时
        from simulation.matcher import fetch_quotes_batch
        if holdings:
            codes = list(holdings.keys())
            quotes = fetch_quotes_batch(codes)
            for code, info in holdings.items():
                q = quotes.get(code, {})
                price = q.get("price", info["avg_cost"])
                if price <= 0:
                    continue
                pnl_pct = price / info["avg_cost"] - 1 if info["avg_cost"] > 0 else 0
                reason = ""
                pnl_amount = (price - info["avg_cost"]) * info["shares"]
                days_held = 0
                if info.get("buy_date"):
                    try:
                        buy_dt = datetime.strptime(info["buy_date"], "%Y-%m-%d")
                        days_held = (datetime.now() - buy_dt).days
                    except ValueError:
                        pass

                if pnl_pct <= self.stop_loss_pct:
                    reason = f"止损({pnl_pct:+.1%}, 持有{days_held}日, {pnl_amount:+,.0f}元)"
                elif pnl_pct >= self.take_profit_pct:
                    reason = f"止盈({pnl_pct:+.1%}, 持有{days_held}日, {pnl_amount:+,.0f}元)"
                elif days_held >= self.max_holding_days and abs(pnl_pct) < 0.03:
                    reason = f"超时调仓预判(已{days_held}日, 收益{pnl_pct:+.1%}, {pnl_amount:+,.0f}元)"

                if reason:
                    plan["sells"].append({
                        "code": code,
                        "name": q.get("name", ""),
                        "shares": info["shares"],
                        "price": price,
                        "reason": reason,
                    })

        # 1.5 调仓逻辑: 对比持仓与候选池，主动换仓
        sell_codes = {s["code"] for s in plan["sells"]}
        remaining_holdings = {c: i for c, i in holdings.items()
                              if c not in sell_codes}
        rotation_done = 0
        max_rotation = 2  # 每天最多调仓2只

        if remaining_holdings and rotation_done < max_rotation:
            try:
                rotation_sells, rotation_holding_scores = (
                    self._evaluate_rotation(remaining_holdings, quotes)
                )
                for rs in rotation_sells:
                    if rotation_done >= max_rotation:
                        break
                    plan["sells"].append(rs)
                    rotation_done += 1
                    print(f"  [调仓卖出] {rs['code']} → {rs['reason']}")
            except Exception as e:
                logger.warning(f"调仓评估失败: {e}")

        # 保存持仓因子分析（供报告使用）
        if remaining_holdings:
            try:
                plan["holding_analysis"] = self._analyze_holdings(
                    remaining_holdings, quotes
                )
            except Exception:
                plan["holding_analysis"] = []

        # 2. 买入计划: 计算可用仓位后选股
        current_count = len(holdings) - len(plan["sells"])
        slots = max(0, self.max_positions - current_count)

        # 预计卖出回笼资金
        sell_cash = sum(
            s["shares"] * s["price"]
            for s in plan["sells"]
        )
        available = cash + sell_cash

        if slots > 0 and available >= 5000:
            try:
                from portfolio.allocator import get_stock_picks_live
                exclude = [c for c in holdings.keys()
                           if c not in [s["code"] for s in plan["sells"]]]
                picks = get_stock_picks_live(
                    stock_capital=available,
                    top_n=slots,
                    exclude_codes=exclude,
                )
                for p in picks:
                    plan["buys"].append({
                        "code": p["code"],
                        "name": p.get("name", ""),
                        "shares": p["shares"],
                        "price": p["price"],
                        "amount": p["amount"],
                        "reason": p.get("reason", ""),
                    })
            except Exception as e:
                logger.warning(f"选股失败: {e}")

        # 生成决策摘要（无操作理由）
        notes = []
        if not plan["sells"] and not plan["buys"]:
            if not holdings:
                notes.append("空仓且资金不足5000元，无法建仓")
            elif slots == 0:
                # 持仓已满，说明每只持仓的理由
                from simulation.matcher import fetch_quotes_batch
                qts = fetch_quotes_batch(list(holdings.keys())) if holdings else {}
                hold_notes = []
                for code, info in holdings.items():
                    q = qts.get(code, {})
                    p = q.get("price", info["avg_cost"])
                    pct = (p / info["avg_cost"] - 1) if info["avg_cost"] > 0 else 0
                    hold_notes.append(f"{code}({pct:+.1%})")
                notes.append(f"仓位已满({len(holdings)}只)，均未触发止损/止盈/超时")
            elif available < 5000:
                notes.append(f"可用资金{available:,.0f}元不足5000元，无法建仓")
            else:
                notes.append("选股未产生合格标的")
        elif not plan["sells"]:
            if holdings:
                from simulation.matcher import fetch_quotes_batch
                qts = fetch_quotes_batch(list(holdings.keys())) if holdings else {}
                hold_status = []
                for code, info in holdings.items():
                    q = qts.get(code, {})
                    p = q.get("price", info["avg_cost"])
                    pct = (p / info["avg_cost"] - 1) if info["avg_cost"] > 0 else 0
                    hold_status.append(f"{code}({pct:+.1%})")
                notes.append(f"持仓正常: {', '.join(hold_status)}")
        if notes:
            plan["decision_note"] = "; ".join(notes)

        # 保存计划
        with open(self._plan_file, "w") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

        sell_str = ", ".join(f"{s['code']}({s['reason']})" for s in plan["sells"])
        buy_str = ", ".join(f"{b['code']}({b.get('name', '')})" for b in plan["buys"])
        print(f"  明日计划: 卖[{sell_str or '无'}] 买[{buy_str or '无'}]")

    def _push_daily_report(self):
        """推送当日报告（仅交易日 15:00 后）

        硬性守卫: 周六/周日 + 法定节假日 + 调休补班的周六，全部跳过推送。
        """
        now = datetime.now()
        if now.weekday() >= 5:
            print("  周末跳过推送")
            return
        if not is_trading_day() or now.hour < 15:
            print("  非交易时间，跳过推送")
            return
        try:
            from simulation.report import daily_report, format_sim_push_message
            from alert.notify import send_to_all
            report = daily_report(push_format=True)
            title, content = format_sim_push_message(report)
            send_to_all(title, content)
            print("  日报已推送")
        except Exception as e:
            logger.warning(f"推送失败: {e}")

    # ============ 常驻进程 ============

    def start(self, push: bool = False):
        """
        启动盘中交易引擎

        流程: 盘前准备 → 盘中每3分钟撮合 → 收盘结算推送 → 自动退出
        必须在交易日运行，非交易日（周末/法定节假日）直接退出
        """
        if not is_trading_day():
            print("今天不是交易日，退出")
            return

        print(f"\n{'='*50}")
        print(f"模拟盘引擎启动 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*50}")
        print(self.status())

        self._running = True

        # 信号处理
        def _signal_handler(sig, frame):
            print("\n收到退出信号，正在停止...")
            self._running = False

        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)

        # ======== Phase 1: 盘前准备 ========
        now = datetime.now()
        time_str = now.strftime("%H:%M")
        if time_str < "09:25":
            wait_sec = (datetime(now.year, now.month, now.day, 9, 25) - now).total_seconds()
            if wait_sec > 0:
                print(f"等待至 09:25 开始盘前准备... ({int(wait_sec)}秒)")
                time.sleep(wait_sec)

        try:
            self.on_pre_open()
        except Exception as e:
            logger.error(f"盘前准备失败: {e}")

        # ======== Phase 2: 盘中交易 (09:30 ~ 14:58) ========
        print("\n等待开盘 (09:30)...")
        now = datetime.now()
        open_time = datetime(now.year, now.month, now.day, 9, 30)
        if now < open_time:
            time.sleep((open_time - now).total_seconds())

        print(f"\n=== 开盘 {datetime.now().strftime('%H:%M')} 开始盘中轮询 ===")

        bar_count = 0
        while self._running:
            now = datetime.now()
            time_str = now.strftime("%H:%M")

            # 11:30-12:59 午休，不轮询（但也不退出）
            if "11:30" <= time_str < "13:00":
                time.sleep(30)
                continue

            # 14:58 停止盘中轮询
            if time_str >= "14:58":
                break

            # 盘中撮合
            try:
                self.on_bar()
                bar_count += 1
            except Exception as e:
                logger.error(f"盘中轮询失败: {e}")

            time.sleep(BAR_INTERVAL_SECONDS)

        print(f"盘中轮询结束，共 {bar_count} 轮")

        # ======== Phase 3: 收盘结算 ========
        print("\n等待收盘 (15:00)...")
        now = datetime.now()
        close_time = datetime(now.year, now.month, now.day, 15, 0)
        if now < close_time:
            time.sleep((close_time - now).total_seconds())

        try:
            self.on_after_close(push=push)
        except Exception as e:
            logger.error(f"收盘结算失败: {e}")

        print(f"\n模拟盘引擎今日任务完成 {datetime.now().strftime('%H:%M')}")

    def run_once(self, push: bool = False):
        """
        离线测试模式：用当前行情快照一次性撮合
        注意：此模式不在交易时间内运行，价格非真实盘中价，仅供调试
        正式交易请用 --start
        """
        print(f"\n{'='*50}")
        print(f"模拟盘离线测试 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"注意: 非盘中交易，价格仅供参考")
        print(f"{'='*50}")

        # 非交易时间强制不推送
        push = False

        # 刷新持仓
        self.portfolio = self.load_portfolio()

        # 1. 加载计划
        self.on_pre_open()

        # 2. 立即撮合所有订单（先卖后买，确保资金正确）
        if self.orders_pending:
            print("\n撮合待执行订单...")
            from simulation.matcher import fetch_quotes_batch

            # 先卖后买
            sell_orders = [o for o in self.orders_pending if o.side == "sell"]
            buy_orders = [o for o in self.orders_pending if o.side == "buy"]

            if sell_orders:
                codes = list(set(o.symbol for o in sell_orders))
                quotes = fetch_quotes_batch(codes)
                for order in sell_orders:
                    q = quotes.get(order.symbol, {})
                    if q.get("price", 0) <= 0:
                        order.status = "rejected"
                        self.update_order_status(order)
                        continue
                    self.matcher.match(order, q)
                    if order.status == "filled":
                        self._execute_order(order, q)
                    else:
                        self.update_order_status(order)

            if buy_orders:
                codes = list(set(o.symbol for o in buy_orders))
                quotes = fetch_quotes_batch(codes)
                for order in buy_orders:
                    # 资金不足则跳过（不是reject，后续可能有钱了再试）
                    q = quotes.get(order.symbol, {})
                    if q.get("price", 0) <= 0:
                        order.status = "rejected"
                        self.update_order_status(order)
                        continue
                    self.matcher.match(order, q)
                    if order.status == "filled":
                        self._execute_order(order, q)
                    else:
                        self.update_order_status(order)

            self.orders_pending = [o for o in self.orders_pending
                                   if o.status == "pending"]

        # 3. 止损检查
        from simulation.matcher import fetch_quotes_batch
        holdings = self.portfolio.get("holdings", {})
        if holdings:
            quotes = fetch_quotes_batch(list(holdings.keys()))
            for code, info in list(holdings.items()):
                # 跳过已被前序止损卖出的
                if code not in self.portfolio.get("holdings", {}):
                    continue
                q = quotes.get(code, {})
                if q.get("price", 0) <= 0:
                    continue
                # T+1 检查
                if not self.matcher.can_sell_today(info.get("buy_date", "")):
                    continue
                stop_order = self.matcher.check_stop_loss(
                    code, info["shares"], info["avg_cost"],
                    info.get("buy_date", ""), q,
                    self.stop_loss_pct, self.take_profit_pct,
                    self.max_holding_days,
                )
                if stop_order:
                    self.matcher.match(stop_order, q)
                    if stop_order.status == "filled":
                        self._execute_order(stop_order, q)

        # 4. 收盘结算
        self.on_after_close(push=push)
