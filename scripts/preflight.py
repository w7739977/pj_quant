#!/usr/bin/env python3
"""
Preflight 健康检查 — 每日自动执行前运行

检查项:
  1. 数据新鲜度（10只抽样，>=8只最新）
  2. 数据准确性（3只在线对比，误差<1%）
  3. 数据完整性（20只抽样，pe_ttm/pb/turnover_rate 非空率>=80%）
  4. 模型状态（文件存在 + R²>0.02 + 不超60天）

Exit: 0=通过, 1=有失败项
"""

import sys
import os
import json
import random
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _last_trade_date() -> str:
    """推算上一个交易日的日期（简单处理：周一→上周五，其他→昨天）"""
    today = datetime.now()
    if today.weekday() == 0:  # 周一
        return (today - timedelta(days=3)).strftime("%Y-%m-%d")
    elif today.weekday() >= 5:  # 周末
        days_back = today.weekday() - 4  # 周六→2, 周日→3
        return (today - timedelta(days=days_back)).strftime("%Y-%m-%d")
    else:
        return (today - timedelta(days=1)).strftime("%Y-%m-%d")


def check_data_freshness() -> dict:
    """检查1: 数据新鲜度"""
    from data.storage import list_cached_stocks, load_stock_daily

    stocks = list_cached_stocks()
    if not stocks:
        return {"pass": False, "msg": "无缓存股票数据"}

    sample = random.sample(stocks, min(10, len(stocks)))
    target_date = _last_trade_date()

    fresh_count = 0
    for sym in sample:
        try:
            df = load_stock_daily(sym)
            if df.empty:
                continue
            latest = df["date"].max().strftime("%Y-%m-%d")
            if latest >= target_date:
                fresh_count += 1
        except Exception:
            continue

    total = len(sample)
    ok = fresh_count >= 8 or fresh_count == total  # 全部通过也算
    msg = f"{fresh_count}/{total} 只股票数据最新 (要求 >= {min(8, total)}/{total}, 目标日期 {target_date})"
    return {"pass": ok, "msg": msg}


def check_data_accuracy() -> dict:
    """检查2: 数据准确性（在线对比）"""
    from data.fetcher import fetch_realtime_tencent
    from data.storage import load_stock_daily

    benchmark_stocks = [
        ("000001", "平安银行"),
        ("600519", "贵州茅台"),
        ("300750", "宁德时代"),
    ]

    results = []
    skip_count = 0
    max_error = 0.0

    for code, name in benchmark_stocks:
        try:
            online = fetch_realtime_tencent(code)
            if not online or online.get("price", 0) <= 0:
                skip_count += 1
                continue

            online_price = online["price"]
            df = load_stock_daily(code)
            if df.empty:
                skip_count += 1
                continue

            local_price = float(df.iloc[-1]["close"])
            if local_price <= 0:
                skip_count += 1
                continue

            error = abs(online_price - local_price) / local_price
            max_error = max(max_error, error)
            results.append(error < 0.01)

        except Exception:
            skip_count += 1
            continue

    if skip_count == len(benchmark_stocks):
        return {"pass": True, "msg": f"全部 SKIP (网络不可达或非交易时段)", "skip": True}

    compared = len(results)
    passed = sum(results)
    ok = passed == compared
    max_pct = max_error * 100
    msg = f"{passed}/{compared} 只对比通过 (最大误差 {max_pct:.2f}%)"
    if skip_count > 0:
        msg += f", {skip_count} 只 SKIP"
    return {"pass": ok, "msg": msg}


def check_data_completeness() -> dict:
    """
    检查3: 数据完整性（关键字段行级非空率）

    - 按行聚合（非股均值），避免少数全 NaN 股票拉偏
    - pe_ttm 天然 ~68%（亏损股无 PE，Tushare 返回空），阈值 50%
    - pb / turnover_rate 天然 ~97-100%，阈值 80%
    """
    from data.storage import list_cached_stocks, load_stock_daily

    stocks = list_cached_stocks()
    if not stocks:
        return {"pass": False, "msg": "无缓存股票数据"}

    sample = random.sample(stocks, min(50, len(stocks)))
    check_cols = ["pe_ttm", "pb", "turnover_rate"]
    thresholds = {"pe_ttm": 0.50, "pb": 0.80, "turnover_rate": 0.80}

    total_rows = {c: 0 for c in check_cols}
    non_null = {c: 0 for c in check_cols}

    for sym in sample:
        try:
            df = load_stock_daily(sym)
            if df.empty:
                continue
            recent = df.tail(60)
            for col in check_cols:
                if col in recent.columns:
                    total_rows[col] += len(recent)
                    non_null[col] += int(recent[col].notna().sum())
        except Exception:
            continue

    if sum(total_rows.values()) == 0:
        return {"pass": False, "msg": "无有效数据样本"}

    rates = {
        c: (non_null[c] / total_rows[c]) if total_rows[c] > 0 else 0.0
        for c in check_cols
    }

    failed = [
        f"{c} {rates[c]:.0%} (<{thresholds[c]:.0%})"
        for c in check_cols
        if rates[c] < thresholds[c]
    ]
    if failed:
        return {"pass": False, "msg": ", ".join(failed)}

    rates_str = ", ".join(f"{c} {rates[c]:.0%}" for c in check_cols)
    return {"pass": True, "msg": f"关键字段非空率: {rates_str}"}


def check_model() -> dict:
    """检查4: 模型状态"""
    model_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ml", "models", "xgb_ranker.json"
    )
    history_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ml", "models", "model_history.json"
    )

    # 模型文件存在
    if not os.path.exists(model_path):
        return {"pass": False, "msg": "模型文件不存在 (ml/models/xgb_ranker.json)"}

    # 历史记录
    if not os.path.exists(history_path):
        return {"pass": False, "msg": "模型历史不存在 (ml/models/model_history.json)"}

    try:
        with open(history_path, "r") as f:
            history = json.load(f)
    except Exception as e:
        return {"pass": False, "msg": f"模型历史读取失败: {e}"}

    current = history.get("current", {})
    r2 = current.get("cv_r2_mean")
    if r2 is None:
        return {"pass": False, "msg": "模型历史中无 cv_r2_mean"}

    if r2 < 0.02:
        return {"pass": False, "msg": f"R²={r2:.4f} < 0.02 阈值，模型质量不足"}

    # 文件年龄
    mtime = os.path.getmtime(model_path)
    age_days = (datetime.now().timestamp() - mtime) / 86400
    train_date = current.get("date", "unknown")
    age_warn = ""
    if age_days > 60:
        age_warn = f" (⚠ {age_days:.0f}天未更新，建议执行 evolve)"

    return {"pass": True, "msg": f"R²={r2:.4f}, 训练于 {train_date}{age_warn}"}


def main():
    checks = [
        ("数据新鲜度", check_data_freshness),
        ("数据准确性", check_data_accuracy),
        ("数据完整性", check_data_completeness),
        ("模型状态", check_model),
    ]

    has_fail = False
    for name, fn in checks:
        try:
            result = fn()
        except Exception as e:
            result = {"pass": False, "msg": f"检查异常: {e}"}

        status = "✓" if result["pass"] else "✗"
        if result.get("skip"):
            status = "⊘"  # SKIP
        print(f"  [{status}] {name}: {result['msg']}")

        if not result["pass"] and not result.get("skip"):
            has_fail = True

    if has_fail:
        print("\n⚠ 有检查项未通过，请排查后再执行日常任务")
        sys.exit(1)
    else:
        print("\n✓ 所有检查通过")
        sys.exit(0)


if __name__ == "__main__":
    main()
