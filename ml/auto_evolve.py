"""
自动进化模块 — 闭环迭代

每月自动执行一次:
1. 更新股票池 + 行情数据
2. 重新计算因子（含情绪）
3. 训练新模型
4. 回测对比新旧模型
5. 新模型更优 → 自动替换；否则保留旧模型
6. 微信推送进化报告

用法:
  python main.py evolve          # 手动触发
  python main.py evolve --push   # 触发 + 推送报告
  # 或加入 crontab 每月1号执行
"""

import os
import json
import time
import logging
import numpy as np
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)

EVOLVE_LOG_DIR = "logs/evolve"


def _ensure_dirs():
    os.makedirs(EVOLVE_LOG_DIR, exist_ok=True)


def evolve(push: bool = False) -> dict:
    """
    执行一次自动进化

    Returns
    -------
    dict: 完整进化报告
    """
    from ml.ranker import (
        train_model, get_model_info, PRODUCTION_MODEL, FEATURE_COLS,
    )
    from factors.calculator import compute_stock_pool_factors, _batch_sentiment_factors
    from factors.data_loader import get_stock_daily, get_small_cap_stocks
    from config.settings import INITIAL_CAPITAL

    _ensure_dirs()

    report = {
        "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "steps": {},
        "decision": "",
    }

    print("=" * 60)
    print(f"自动进化开始 - {report['start_time']}")
    print("=" * 60)

    # ============ Step 1: 旧模型基准 ============
    print("\n[1/5] 获取旧模型基准...")
    old_info = get_model_info()
    old_r2 = old_info.get("current", {}).get("cv_r2_mean", None)
    print(f"  当前模型 R²: {old_r2}")
    print(f"  历史版本数: {old_info.get('version_count', 0)}")
    report["steps"]["old_model"] = {
        "old_r2": old_r2,
        "version_count": old_info.get("version_count", 0),
    }

    # ============ Step 2: 获取股票池 + 行情数据 ============
    print("\n[2/5] 获取股票池 + 行情数据...")
    pool = get_small_cap_stocks()
    if pool.empty:
        # AKShare 限流时，使用 BaoStock 获取沪深主板股票列表
        import baostock as bs
        lg = bs.login()
        rs = bs.query_stock_basic()
        stock_list = []
        while rs.error_code == "0" and rs.next():
            row = rs.get_row_data()
            # row: [code, name, ipoDate, outDate, type, tradeStatus]
            # type: 1=股票, 2=指数; tradeStatus: 1=上市
            code = row[0] if len(row) > 0 else ""
            name = row[1] if len(row) > 1 else ""
            stype = row[4] if len(row) > 4 else ""
            trade_status = row[5] if len(row) > 5 else ""
            # type=1(股票) + tradeStatus=1(上市), 排除 ST/退市/北交所/科创板
            if stype == "1" and trade_status == "1" and "ST" not in name and "退" not in name:
                pure_code = code.split(".")[-1] if "." in code else code
                if pure_code.startswith(("8", "688", "9")):
                    continue
                stock_list.append(pure_code)
        bs.logout()
        symbols = stock_list  # 全量股票
    else:
        symbols = pool["code"].tolist()
    print(f"  股票池: {len(symbols)} 只")
    report["steps"]["stock_pool"] = {"count": len(symbols)}

    if len(symbols) < 20:
        report["decision"] = "ABORT: 股票池不足 20 只，无法训练"
        print(f"  ✗ {report['decision']}")
        return _finish_report(report, push)

    # ============ Step 3: 滚动因子计算 ============
    print("\n[3/5] 滚动计算因子（含情绪）...")
    import baostock as bs

    bs.login()
    records = []
    success = 0
    for i, sym in enumerate(symbols[:200]):  # 最多 200 只，控制时间
        try:
            prefix = "sh" if sym.startswith("6") else "sz"
            rs = bs.query_history_k_data_plus(
                f"{prefix}.{sym}",
                "date,open,high,low,close,volume,turn,pctChg",
                start_date="2024-06-01", end_date=datetime.now().strftime("%Y-%m-%d"),
                frequency="d", adjustflag="2",
            )
            rows = []
            while rs.error_code == "0" and rs.next():
                rows.append(rs.get_row_data())
            if not rows:
                continue

            df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close",
                                              "volume", "turnover", "pct_chg"])
            for c in ["open", "high", "low", "close", "volume", "turnover", "pct_chg"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df["date"] = pd.to_datetime(df["date"])

            if len(df) < 80:
                continue

            from factors.calculator import (
                calc_momentum, calc_volatility, calc_turnover_factor,
                calc_volume_price, calc_technical,
            )

            # 滚动截面，每 20 天一个样本
            for end_idx in range(60, len(df) - 20, 20):
                window = df.iloc[:end_idx + 1]
                fwd = df.iloc[end_idx:end_idx + 21]
                if len(fwd) < 21:
                    continue
                forward_return = float(fwd.iloc[-1]["close"]) / float(fwd.iloc[0]["close"]) - 1.0

                factors = {"code": sym}
                factors.update(calc_momentum(window))
                factors.update(calc_volatility(window))
                factors.update(calc_turnover_factor(window))
                factors.update(calc_volume_price(window))
                factors.update(calc_technical(window))
                for col in ["pe_ttm", "pb", "turnover_rate", "volume_ratio"]:
                    factors[col] = np.nan
                records.append({"code": sym, "label": forward_return, **factors})
            success += 1
        except Exception:
            continue

        if (i + 1) % 50 == 0:
            print(f"  已处理 {i+1}/{min(len(symbols), 200)}")

    bs.logout()

    train_df = pd.DataFrame(records)
    print(f"  训练样本: {len(train_df)} 条 ({success} 只股票)")
    report["steps"]["factors"] = {
        "train_samples": len(train_df),
        "stocks_processed": success,
    }

    if len(train_df) < 50:
        report["decision"] = f"ABORT: 训练样本不足 ({len(train_df)} < 50)"
        print(f"  ✗ {report['decision']}")
        return _finish_report(report, push)

    # 批量情绪打标
    print("  计算情绪因子...")
    unique_codes = train_df["code"].unique()
    mini_df = pd.DataFrame({"code": unique_codes})
    sent_df = _batch_sentiment_factors(mini_df)
    sent_map = dict(zip(sent_df["code"], sent_df["sentiment_score"]))
    train_df["sentiment_score"] = train_df["code"].map(sent_map).fillna(0)
    has_sent = sum(1 for v in sent_map.values() if v != 0)
    print(f"  情绪数据: {has_sent}/{len(unique_codes)} 只有新闻")
    report["steps"]["factors"]["sentiment_coverage"] = f"{has_sent}/{len(unique_codes)}"

    # ============ Step 4: 训练新模型（自动对比） ============
    print("\n[4/5] 训练新模型（自动对比旧模型）...")
    result = train_model(train_df)

    if not result:
        report["decision"] = "ABORT: 训练失败"
        print(f"  ✗ {report['decision']}")
        return _finish_report(report, push)

    new_r2 = result["cv_r2_mean"]
    is_best = result.get("is_new_best", True)

    report["steps"]["training"] = {
        "new_r2": new_r2,
        "new_r2_std": result["cv_r2_std"],
        "train_samples": result["train_samples"],
        "is_new_best": is_best,
        "old_r2": old_r2,
    }

    if is_best:
        print(f"  ✓ 新模型 R²={new_r2:.4f} ≥ 旧模型 R²={old_r2}，已上线!")
    else:
        print(f"  → 新模型 R²={new_r2:.4f} < 旧模型 R²={old_r2}，保留旧模型")

    # ============ Step 5: 因子重要性变化 ============
    print("\n[5/5] 因子分析...")
    importance = result.get("feature_importance", {})
    sent_rank = list(importance.keys()).index("sentiment_score") + 1 if "sentiment_score" in importance else "N/A"
    print(f"  情绪因子排名: #{sent_rank}/{len(importance)}")
    print(f"  Top 5 因子:")
    for i, (f, v) in enumerate(list(importance.items())[:5], 1):
        print(f"    {i}. {f}: {v:.4f}")

    report["steps"]["factors"]["top5"] = list(importance.items())[:5]
    report["steps"]["factors"]["sentiment_rank"] = sent_rank

    # ============ 决策总结 ============
    report["decision"] = "NEW_MODEL_DEPLOYED" if is_best else "OLD_MODEL_RETAINED"
    report["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"\n{'='*60}")
    print(f"进化决策: {'✓ 新模型已上线' if is_best else '→ 保留旧模型'}")
    print(f"新 R²={new_r2:.4f} vs 旧 R²={old_r2}")
    print(f"{'='*60}")

    # 保存进化日志
    _save_evolve_log(report)

    # 推送
    if push:
        _push_report(report)

    return report


def _finish_report(report: dict, push: bool) -> dict:
    report["end_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _save_evolve_log(report)
    if push:
        _push_report(report)
    return report


def _save_evolve_log(report: dict):
    """保存进化日志"""
    _ensure_dirs()
    path = os.path.join(EVOLVE_LOG_DIR, f"evolve_{datetime.now().strftime('%Y%m%d_%H%M')}.json")
    with open(path, "w") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    logger.info(f"进化日志已保存: {path}")


def _push_report(report: dict):
    """微信推送进化报告"""
    try:
        from alert.notify import send_to_all
    except ImportError:
        print("推送模块不可用，跳过")
        return

    decision = report["decision"]
    steps = report["steps"]

    if "NEW_MODEL_DEPLOYED" in decision:
        emoji = "✓ 新模型已上线"
    elif "OLD_MODEL_RETAINED" in decision:
        emoji = "→ 保留旧模型"
    else:
        emoji = f"✗ {decision}"

    training = steps.get("training", {})
    new_r2 = training.get("new_r2", "N/A")
    old_r2 = training.get("old_r2", "N/A")
    samples = training.get("train_samples", "N/A")
    factors = steps.get("factors", {})

    title = f"模型进化报告 ({emoji})"
    msg = f"""**模型进化报告**
时间: {report.get('start_time', '')}

**决策: {emoji}**

模型对比:
- 旧模型 R²: {old_r2}
- 新模型 R²: {new_r2}
- 训练样本: {samples}

数据概况:
- 股票池: {steps.get('stock_pool', {}).get('count', 'N/A')} 只
- 训练样本: {factors.get('train_samples', 'N/A')} 条
- 情绪覆盖: {factors.get('sentiment_coverage', 'N/A')}

Top 5 因子:
{chr(10).join(f'  {i+1}. {f}: {v:.4f}' for i, (f, v) in enumerate(factors.get('top5', [])))}"""

    send_to_all(title, msg)
    print("进化报告已推送到微信")


def get_evolve_history(limit: int = 5) -> list:
    """查看最近的进化记录"""
    if not os.path.exists(EVOLVE_LOG_DIR):
        return []

    logs = []
    for f in sorted(os.listdir(EVOLVE_LOG_DIR), reverse=True):
        if f.startswith("evolve_") and f.endswith(".json"):
            try:
                with open(os.path.join(EVOLVE_LOG_DIR, f), "r") as fh:
                    logs.append(json.load(fh))
                if len(logs) >= limit:
                    break
            except Exception:
                continue
    return logs
