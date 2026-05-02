"""
A股量化交易系统 - 主入口

用法:
  python main.py backtest           # ETF 轮动策略回测
  python main.py smallcap           # 小市值选股推荐
  python main.py sentiment          # 市场情绪分析
  python main.py train              # 训练/更新 ML 模型
  python main.py predict            # ML 模型预测选股
  python main.py fetch              # 下载 ETF 历史数据
  python main.py fetch-all [--limit N] [--refresh]  # 批量下载全市场股票日线
  python main.py portfolio          # 查看持仓（含实时盈亏）
  python main.py portfolio --buy CODE --shares N --price X    # 记录买入
  python main.py portfolio --sell CODE --price X              # 记录卖出
  python main.py portfolio --cash AMOUNT                      # 修改可用资金
  python main.py portfolio --reset                             # 重置为初始状态
  python main.py deploy [--push] [--simulate]  # 统一部署（生成今日操作清单）
  python main.py live [--push] [--simulate]    # 激进实盘（100%个股，3只集中持仓）
  python main.py evolve [--push]    # 自动进化（训练+对比+替换+报告）
  python main.py evolve-history     # 查看进化记录
  python main.py sim                    # 查看模拟盘状态
  python main.py sim --start [--push]   # 启动模拟盘常驻进程
  python main.py sim --run-once [--push]# 单次执行（测试）
  python main.py sim --report           # 绩效报告
  python main.py sim --report --weekly  # 周报
  python main.py sim --reset            # 重置模拟盘
  python main.py sim --history          # 历史交易记录
  python main.py performance [--push]  # 信号绩效追踪报告
"""

import sys
import os
import logging
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def fetch_data():
    """下载所有 ETF 历史数据（多数据源自动降级 + 本地缓存）"""
    from data.fetcher import fetch_etf_daily
    from data.storage import save_daily_data
    from config.settings import ETF_POOL, BACKTEST_START, BACKTEST_END

    print("开始下载 ETF 历史数据...")
    for symbol, name in ETF_POOL.items():
        try:
            df = fetch_etf_daily(symbol, BACKTEST_START, BACKTEST_END)
            if len(df) > 0:
                save_daily_data(df, symbol)
                print(f"  ✓ {name}({symbol}): {len(df)} 条")
            else:
                print(f"  ✗ {name}({symbol}): 无数据")
        except Exception as e:
            print(f"  ✗ {name}({symbol}): {e}")


def run_backtest():
    """运行 ETF 轮动策略回测（纯本地数据，无需联网）"""
    from data.storage import save_backtest_result, load_daily_data
    from strategy.etf_rotation import ETFRotationStrategy
    from backtest.engine import run_backtest
    from config.settings import ETF_POOL, INITIAL_CAPITAL
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    print("正在加载本地数据...")
    price_data = {}
    for symbol, name in ETF_POOL.items():
        df = load_daily_data(symbol)
        if len(df) > 0:
            price_data[symbol] = df
            print(f"  ✓ {name}({symbol}): {len(df)} 条 ({df['date'].min().strftime('%Y-%m-%d')} ~ {df['date'].max().strftime('%Y-%m-%d')})")
        else:
            print(f"  ✗ {name}({symbol}): 无本地数据，请先运行 python main.py fetch")

    if not price_data:
        print("错误: 无本地数据，请先运行 python main.py fetch 下载数据")
        return

    if not price_data:
        print("错误: 未获取到任何数据")
        return

    print("\n正在生成交易信号...")
    strategy = ETFRotationStrategy()
    signals = strategy.generate_signals(price_data)
    print(f"  共生成 {len(signals)} 条交易信号")

    if len(signals) == 0:
        print("无交易信号")
        return

    print("\n正在运行回测...")
    result = run_backtest(price_data, signals, INITIAL_CAPITAL)

    trades = result["trades"]
    total_cost = sum(t.commission + t.stamp_tax + t.transfer_fee for t in trades)
    result["stats"]["total_cost"] = round(total_cost, 2)

    sell_trades = [t for t in trades if t.action == "sell"]
    if sell_trades:
        wins = sum(
            1 for st in sell_trades
            if any(t.action == "buy" and t.symbol == st.symbol and st.price > t.price for t in trades)
        )
        result["stats"]["win_rate"] = wins / len(sell_trades)

    nav_df = result["nav_curve"]
    if len(nav_df) > 1:
        fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
        axes[0].plot(nav_df["date"], nav_df["nav"], label="Strategy NAV", color="blue", linewidth=1.5)
        axes[0].axhline(y=INITIAL_CAPITAL, color="gray", linestyle="--", alpha=0.5)
        axes[0].set_title(f"ETF Rotation | Annual: {result['stats']['annual_return']:.1%} | MaxDD: {result['stats']['max_drawdown']:.1%}")
        axes[0].set_ylabel("NAV")
        axes[0].legend()
        axes[0].grid(True, alpha=0.3)

        nav = nav_df["nav"].values
        peak = pd.Series(nav).cummax()
        drawdown = (nav - peak) / peak * 100
        axes[1].fill_between(nav_df["date"], drawdown, 0, color="red", alpha=0.3)
        axes[1].set_ylabel("Drawdown (%)")
        axes[1].set_xlabel("Date")
        axes[1].grid(True, alpha=0.3)

        plt.tight_layout()
        output_path = os.path.join(os.path.dirname(__file__), "backtest_result.png")
        plt.savefig(output_path, dpi=150)
        plt.close()
        print(f"\n净值曲线已保存: {output_path}")

    if trades:
        print(f"\n交易明细:")
        print(f"{'Date':<12} {'Action':<6} {'Symbol':<8} {'Price':>8} {'Shares':>6} {'Cost':>8} {'Cash':>10}")
        print("-" * 70)
        for t in trades:
            cost = t.commission + t.stamp_tax + t.transfer_fee
            print(f"{t.date:<12} {t.action:<6} {t.symbol:<8} {t.price:>8.3f} {t.shares:>6} {cost:>8.2f} {t.cash_after:>10.2f}")

    save_backtest_result(nav_df, strategy.name)
    print(f"\n回测结果已保存")


def run_smallcap():
    """小市值选股推荐"""
    from strategy.small_cap import SmallCapStrategy

    print("正在计算小市值选股...")
    strategy = SmallCapStrategy(top_n=10)
    rec = strategy.get_portfolio_recommendation()

    if not rec["stocks"]:
        print("未选出任何股票")
        return

    print(f"\n{'='*60}")
    print(f"小市值多因子选股推荐 ({strategy.name})")
    print(f"{'='*60}")
    print(f"策略: 选出市值 5~50 亿、综合得分最高的 {rec['total_stocks']} 只股票")
    print(f"建议每只分配: {rec['per_stock_capital']:,.0f} 元")
    print(f"{'─'*60}")
    print(f"{'序号':<4} {'代码':<8} {'综合得分':>8} {'建议金额':>10}")
    print(f"{'─'*60}")
    for i, s in enumerate(rec["stocks"], 1):
        print(f"  {i:<3} {s['code']:<8} {s['score']:>8.3f} {s['suggested_amount']:>10,.0f}")
    print(f"{'='*60}")
    print(f"提示: 以上仅为量化策略推荐，不构成投资建议")
    print(f"      建议先观察1-2周信号准确度再决定是否实盘")


def run_sentiment():
    """市场情绪分析"""
    from sentiment.analyzer import analyze_market_sentiment

    print("正在分析市场情绪...")
    result = analyze_market_sentiment()

    print(f"\n{'='*60}")
    print(f"市场情绪报告 ({datetime.now().strftime('%Y-%m-%d')})")
    print(f"{'='*60}")
    print(f"综合情绪得分: {result['score']:+.3f} ({'偏多' if result['score'] > 0 else '偏空' if result['score'] < 0 else '中性'})")
    print(f"分析模式: {result['mode']}")
    print(f"分析新闻数量: {result['news_count']}")
    print(f"正面新闻占比: {result['positive_ratio']:.1%}")
    print(f"{'─'*60}")
    print("重点新闻:")
    for n in result.get("top_news", []):
        tag = "利多" if n["sentiment"] > 0 else "利空" if n["sentiment"] < 0 else "中性"
        print(f"  [{tag}] {n['title'][:50]}... (情绪: {n['sentiment']:+.2f})")

    # GLM-5 深度分析
    deep = result.get("deep_analysis")
    if deep:
        print(f"{'─'*60}")
        print("AI 深度研判 (GLM-5):")
        if deep.get("theme"):
            print(f"  主线: {deep['theme']}")
        if deep.get("analysis"):
            print(f"  分析: {deep['analysis']}")
        if deep.get("action"):
            print(f"  建议: {deep['action']}")
        if deep.get("risks"):
            print(f"  风险: {', '.join(deep['risks'])}")
        if deep.get("adjusted_score") is not None:
            print(f"  修正分数: {deep['adjusted_score']:+.3f}")
    print(f"{'='*60}")


def run_train():
    """训练 ML 模型"""
    from factors.calculator import compute_stock_pool_factors
    from ml.ranker import prepare_training_data, train_model

    print("Step 1: 计算因子...")
    factor_df = compute_stock_pool_factors()
    if factor_df.empty:
        print("因子计算失败")
        return
    print(f"  共 {len(factor_df)} 只股票")

    print("\nStep 2: 准备训练数据...")
    train_df = prepare_training_data(factor_df)
    if train_df.empty:
        print("训练数据不足")
        return
    print(f"  有效样本: {len(train_df)}")

    print("\nStep 3: 训练 XGBoost 模型...")
    result = train_model(train_df)
    if not result:
        print("训练失败")
        return

    print(f"\n{'='*50}")
    print(f"模型训练完成")
    print(f"{'='*50}")
    print(f"训练样本: {result['train_samples']}")
    print(f"交叉验证 R²: {result['cv_r2_mean']:.4f} ± {result['cv_r2_std']:.4f}")
    print(f"\nTop 10 重要因子:")
    for i, (feat, imp) in enumerate(list(result["feature_importance"].items())[:10], 1):
        print(f"  {i:>2}. {feat:<20} {imp:.4f}")
    print(f"{'='*50}")


def run_predict():
    """ML 模型预测"""
    from factors.calculator import compute_stock_pool_factors
    from ml.ranker import predict

    print("正在计算因子并预测...")
    factor_df = compute_stock_pool_factors()
    if factor_df.empty:
        print("因子计算失败")
        return

    result = predict(factor_df)
    if result.empty:
        print("预测失败（模型可能未训练）")
        return

    print(f"\n{'='*50}")
    print(f"ML 选股预测 Top 10")
    print(f"{'='*50}")
    top = result.head(10)
    print(f"{'排名':<4} {'代码':<8} {'预测收益':>10}")
    print(f"{'─'*30}")
    for _, row in top.iterrows():
        print(f"  {int(row['rank']):<3} {row['code']:<8} {row['predicted_return']:>+10.4f}")
    print(f"{'='*50}")


def run_portfolio():
    """持仓管理: 查看/手动同步"""
    from portfolio.tracker import PortfolioTracker
    from portfolio.trade_utils import estimate_buy_cost, estimate_sell_cost

    tracker = PortfolioTracker()

    # 子命令
    if "--reset" in sys.argv:
        from config.settings import INITIAL_CAPITAL
        tracker.state = {"cash": INITIAL_CAPITAL, "holdings": {}, "total_value": INITIAL_CAPITAL}
        from data.storage import save_portfolio
        save_portfolio(tracker.state)
        print("持仓已重置为初始状态")
        return

    if "--cash" in sys.argv:
        idx = sys.argv.index("--cash")
        amount = float(sys.argv[idx + 1])
        tracker.set_cash(amount)
        print(f"可用资金已设为: {amount:,.2f} 元")
        print(tracker.get_realtime_summary())
        return

    if "--buy" in sys.argv:
        idx = sys.argv.index("--buy")
        code = sys.argv[idx + 1]
        shares = int(sys.argv[sys.argv.index("--shares") + 1])
        price = float(sys.argv[sys.argv.index("--price") + 1])
        cost = estimate_buy_cost(price * shares)
        tracker.update_after_buy(code, shares, price, cost)
        print(f"已记录买入: {code} {shares}股 @ {price:.2f} (手续费 {cost:.2f}元)")
        print(tracker.get_realtime_summary())
        return

    if "--sell" in sys.argv:
        idx = sys.argv.index("--sell")
        code = sys.argv[idx + 1]
        if code not in tracker.holdings:
            print(f"错误: {code} 不在持仓中")
            print(f"当前持仓: {', '.join(tracker.holdings.keys()) or '空仓'}")
            return
        price = float(sys.argv[sys.argv.index("--price") + 1])
        shares = tracker.holdings[code]["shares"]
        cost = estimate_sell_cost(price * shares)
        tracker.update_after_sell(code, price, cost)
        print(f"已记录卖出: {code} {shares}股 @ {price:.2f} (手续费 {cost:.2f}元)")
        print(tracker.get_realtime_summary())
        return

    # 默认: 显示持仓
    print(tracker.get_realtime_summary())


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    command = sys.argv[1].lower()

    if command == "backtest":
        run_backtest()
    elif command == "smallcap":
        run_smallcap()
    elif command == "sentiment":
        run_sentiment()
    elif command == "train":
        run_train()
    elif command == "predict":
        run_predict()
    elif command == "fetch":
        fetch_data()
    elif command == "fetch-all":
        _limit = 0
        if "--limit" in sys.argv:
            idx = sys.argv.index("--limit")
            _limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 100
        from data.tushare_daily import run as tushare_daily_run
        _incremental = "--incremental" in sys.argv
        tushare_daily_run(limit=_limit, incremental=_incremental)
    elif command == "fetch-industry":
        from data.tushare_industry import run
        run()
    elif command == "fetch-financial":
        from data.financial_indicator import batch_fetch_all
        _limit = 0
        if "--limit" in sys.argv:
            idx = sys.argv.index("--limit")
            _limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 100
        batch_fetch_all(limit=_limit)
    elif command == "portfolio":
        run_portfolio()
    elif command == "deploy":
        from portfolio.allocator import run_deploy
        push = "--push" in sys.argv
        simulate = "--simulate" in sys.argv
        run_deploy(push=push, simulate=simulate)
    elif command == "live":
        from portfolio.allocator import run_live_deploy
        push = "--push" in sys.argv
        simulate = "--simulate" in sys.argv
        run_live_deploy(push=push, simulate=simulate)
    elif command == "evolve":
        from ml.auto_evolve import evolve
        push = "--push" in sys.argv
        result = evolve(push=push)
        if result:
            print(f"\n进化决策: {result['decision']}")
    elif command == "evolve-history":
        from ml.auto_evolve import get_evolve_history
        history = get_evolve_history(limit=5)
        if not history:
            print("暂无进化记录")
        else:
            print(f"\n最近 {len(history)} 次进化记录:")
            print("=" * 55)
            for h in history:
                dec = h.get("decision", "unknown")
                training = h.get("steps", {}).get("training", {})
                print(f"  {h.get('start_time', '')}")
                print(f"    决策: {dec}")
                print(f"    新R²={training.get('new_r2', 'N/A')} | 旧R²={training.get('old_r2', 'N/A')} | 样本={training.get('train_samples', 'N/A')}")
                print()
    elif command == "performance":
        from scripts.track_performance import run as run_perf
        push = "--push" in sys.argv
        run_perf(push=push)
    elif command == "sim":
        run_sim()
    else:
        print(f"未知命令: {command}")
        print(__doc__)


def run_sim():
    """模拟盘管理"""
    from simulation.engine import SimEngine
    engine = SimEngine()

    if "--reset" in sys.argv:
        engine.reset()
        return

    if "--start" in sys.argv:
        push = "--push" in sys.argv
        engine.start(push=push)
        return

    if "--run-once" in sys.argv:
        push = "--push" in sys.argv
        engine.run_once(push=push)
        return

    if "--history" in sys.argv:
        from simulation.trade_log import get_trades
        trades = get_trades()
        if not trades:
            print("暂无交易记录")
            return
        print(f"\n历史交易记录 (共{len(trades)}笔):")
        print(f"{'日期':<12} {'操作':<6} {'代码':<8} {'名称':<10} "
              f"{'股数':>6} {'价格':>8} {'金额':>10} {'盈亏':>10} {'原因'}")
        print("-" * 90)
        for t in trades:
            profit = f"{t['profit']:+,.0f}" if t.get("profit") else ""
            print(f"{t['date']:<12} {t['side']:<6} {t['symbol']:<8} "
                  f"{t.get('name', ''):<10} {t['shares']:>6} "
                  f"{t['price']:>8.2f} {t['amount']:>10,.0f} "
                  f"{profit:>10} {t.get('reason', '')}")
        return

    if "--report" in sys.argv:
        from simulation.report import weekly_report, daily_report
        if "--weekly" in sys.argv:
            print(weekly_report())
        else:
            print(daily_report())
        return

    # 默认: 显示状态
    print(engine.status())


if __name__ == "__main__":
    main()
