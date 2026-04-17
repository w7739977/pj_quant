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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

# A股交易时间段
MARKET_OPEN = "09:30"
MARKET_CLOSE = "15:00"
BAR_INTERVAL_SECONDS = 180  # 3分钟轮询


def is_trading_day() -> bool:
    """判断今天是否交易日（简单版：周一至周五）"""
    return datetime.now().weekday() < 5


def is_market_hours() -> bool:
    """判断当前是否交易时间"""
    if not is_trading_day():
        return False
    now = datetime.now().strftime("%H:%M")
    return MARKET_OPEN <= now < MARKET_CLOSE


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
        2. 选股生成买入计划
        3. 保存计划文件
        """
        print("\n  生成明日操作计划...")

        # 计算下一个交易日（跳过周末）
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
                if pnl_pct <= self.stop_loss_pct:
                    reason = f"止损({pnl_pct:+.1%})"
                elif pnl_pct >= self.take_profit_pct:
                    reason = f"止盈({pnl_pct:+.1%})"
                elif info.get("buy_date"):
                    try:
                        buy_dt = datetime.strptime(info["buy_date"], "%Y-%m-%d")
                        days = (datetime.now() - buy_dt).days
                        if days >= self.max_holding_days and abs(pnl_pct) < 0.03:
                            reason = f"超时调仓预判(已{days}日)"
                    except ValueError:
                        pass

                if reason:
                    plan["sells"].append({
                        "code": code,
                        "name": q.get("name", ""),
                        "shares": info["shares"],
                        "price": price,
                        "reason": reason,
                    })

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

        # 保存计划
        with open(self._plan_file, "w") as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)

        sell_str = ", ".join(f"{s['code']}({s['reason']})" for s in plan["sells"])
        buy_str = ", ".join(f"{b['code']}({b.get('name', '')})" for b in plan["buys"])
        print(f"  明日计划: 卖[{sell_str or '无'}] 买[{buy_str or '无'}]")

    def _push_daily_report(self):
        """推送当日报告"""
        try:
            from simulation.report import daily_report, format_sim_push_message
            from alert.notify import send_message
            from config.settings import PUSHPLUS_TOKEN
            report = daily_report(push_format=True)
            title, content = format_sim_push_message(report)
            send_message(title, content, PUSHPLUS_TOKEN)
            print("  日报已推送")
        except Exception as e:
            logger.warning(f"推送失败: {e}")

    # ============ 常驻进程 ============

    def start(self, push: bool = False):
        """启动常驻进程"""
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

        pre_open_done = False
        close_done = False
        today_str = ""

        while self._running:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M")

            # 新的一天重置
            if today != today_str:
                today_str = today
                pre_open_done = False
                close_done = False

            if not is_trading_day():
                time.sleep(60)
                continue

            # 09:10 盘前准备
            if not pre_open_done and time_str >= "09:10":
                try:
                    self.on_pre_open()
                except Exception as e:
                    logger.error(f"盘前准备失败: {e}")
                pre_open_done = True

            # 09:33 ~ 14:57 盘中轮询
            if is_market_hours() and time_str >= "09:33":
                try:
                    self.on_bar()
                except Exception as e:
                    logger.error(f"盘中轮询失败: {e}")
                time.sleep(BAR_INTERVAL_SECONDS)
                continue

            # 15:00 收盘结算
            if not close_done and time_str >= "15:00":
                try:
                    self.on_after_close(push=push)
                except Exception as e:
                    logger.error(f"收盘结算失败: {e}")
                close_done = True

            # 非交易时间，降低轮询频率
            time.sleep(30)

        print("模拟盘引擎已停止")

    def run_once(self, push: bool = False):
        """
        单次执行模式（测试用）
        立即执行: 盘前准备 → 盘中撮合 → 收盘结算
        """
        print(f"\n{'='*50}")
        print(f"模拟盘单次执行 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*50}")

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
