"""
自动进化模块 — 闭环迭代

每月自动执行一次:
1. 获取旧模型基准
2. 计算因子（含情绪，纯本地 SQLite）
3. 准备训练数据（滚动截面，纯本地）
4. 训练新模型（自动对比 + 版本管理）
5. 微信推送进化报告

用法:
  python main.py evolve          # 手动触发
  python main.py evolve --push   # 触发 + 推送报告
  # 或加入 crontab 每月1号执行
"""

import os
import json
import logging
import pandas as pd
from datetime import datetime

logger = logging.getLogger(__name__)

EVOLVE_LOG_DIR = "logs/evolve"


def _ensure_dirs():
    os.makedirs(EVOLVE_LOG_DIR, exist_ok=True)


def evolve(push: bool = False) -> dict:
    """
    模型自动进化 — 纯本地数据路径

    流程: 读旧模型 R² → 计算因子(本地SQLite) → 准备训练数据(滚动截面)
        → 训练新模型(自动版本管理) → 推送报告
    """
    from ml.ranker import (
        train_model, get_model_info, PRODUCTION_MODEL, FEATURE_COLS,
    )
    from factors.calculator import compute_stock_pool_factors
    import os  # 用于本函数内 PRODUCTION_MODEL 路径判断

    _ensure_dirs()

    report = {
        "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "steps": {},
        "decision": None,
    }

    print("=" * 60)
    print("模型自动进化")
    print("=" * 60)

    # === Step 1: 旧模型基准 ===
    print("\n[1/4] 获取旧模型基准...")
    old_info = get_model_info()
    old_r2 = old_info.get("current", {}).get("cv_r2_mean")
    print(f"  当前模型 R²: {old_r2}")
    report["steps"]["old_model"] = {
        "old_r2": old_r2,
        "version_count": old_info.get("version_count", 0),
    }

    # === Step 2: 计算因子（跳过情绪 — sentiment_history 表未回填）===
    # sentiment_score 不在 FEATURE_COLS 中，跳过情绪计算可加速 6 倍且不影响训练。
    # 8 维度推送展示仍调实时情绪 API（仅 top 10 推荐，不限流）。
    print("\n[2/4] 计算因子（跳过情绪）...")
    factor_df = compute_stock_pool_factors(skip_sentiment=True)

    if factor_df.empty:
        report["decision"] = "ABORT: 因子计算失败 / 股票池为空"
        print(f"  ✗ {report['decision']}")
        return _finish_report(report, push)

    pool_size = len(factor_df)
    print(f"  股票池: {pool_size} 只")
    report["steps"]["stock_pool"] = {"count": pool_size}
    report["steps"]["factors"] = {}

    if pool_size < 20:
        report["decision"] = f"ABORT: 股票池不足 20 只 ({pool_size})"
        print(f"  ✗ {report['decision']}")
        return _finish_report(report, push)

    # === Step 3: 准备训练数据（滚动截面，纯本地） ===
    print("\n[3/4] 准备训练数据（滚动截面）...")
    from ml.ranker import prepare_training_data
    train_df = prepare_training_data(factor_df)

    if train_df.empty or len(train_df) < 50:
        report["decision"] = f"ABORT: 训练样本不足 ({len(train_df)} < 50)"
        report["steps"]["factors"]["train_samples"] = len(train_df)
        print(f"  ✗ {report['decision']}")
        return _finish_report(report, push)

    # 注: sentiment_score 已从 FEATURE_COLS 移除 (sentiment_history 表未回填)，
    # 训练数据不再含此列；待回填后恢复
    print(f"  训练样本: {len(train_df)} 条")
    report["steps"]["factors"]["train_samples"] = len(train_df)

    # === Step 4: 训练新模型 (defer_promotion, 让 L2 evaluator 决定) ===
    # P1.1 (2026-05-16): 旧逻辑用 R² 判 is_new_best, BDE 实证 R²↓74% 但 L2↑15pp,
    # R² 会阻止 B 这种「真改善」模型上线. 改为 defer_promotion + L2 评估自己 promote.
    print("\n[4/5] 训练新模型 (defer_promotion=True)...")
    result = train_model(train_df, defer_promotion=True)

    if not result:
        report["decision"] = "ABORT: 训练失败"
        print(f"  ✗ {report['decision']}")
        return _finish_report(report, push)

    new_r2 = result["cv_r2_mean"]
    candidate_path = result["candidate_path"]

    report["steps"]["training"] = {
        "new_r2": new_r2,
        "new_r2_std": result["cv_r2_std"],
        "train_samples": result["train_samples"],
        "old_r2": old_r2,
        "candidate_path": candidate_path,
        "top_factors": list(result.get("feature_importance", {}).keys())[:5],
    }

    importance = result.get("feature_importance", {})
    print(f"  R²: 新 {new_r2:.4f} vs 旧 {old_r2} (仅供参考, 不作上线判定)")
    print(f"  Top 5 因子:")
    for i, (f, v) in enumerate(list(importance.items())[:5], 1):
        print(f"    {i}. {f}: {v:.4f}")
    report["steps"]["factors"]["top5"] = list(importance.items())[:5]

    # === Step 5: L2 评估 (从 2024-01-01 起, 跑两个模型对照) ===
    print("\n[5/6] L2 mini-backtest 评估新/旧模型...")
    is_best = _l2_promotion_check(
        candidate_path=candidate_path,
        factor_df=factor_df,
        report=report,
    )

    if is_best:
        # 把 candidate 升为 PRODUCTION_MODEL
        import shutil
        if os.path.exists(PRODUCTION_MODEL):
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(PRODUCTION_MODEL, os.path.join("ml/models", f"xgb_ranker_{ts}.json"))
        shutil.copy2(candidate_path, PRODUCTION_MODEL)
        # 保存 feature_importance + history
        from ml.ranker import IMPORTANCE_PATH, _save_history
        with open(IMPORTANCE_PATH, "w") as f:
            json.dump(importance, f, indent=2, ensure_ascii=False)
        _save_history(new_r2, result["cv_r2_std"], result["train_samples"], importance)
        report["decision"] = "✓ 上线新模型 (L2 排 ST 累计 α 优于旧)"
        print(f"  ✅ 新模型 L2 胜出, 已上线 (R² {old_r2}→{new_r2})")
    else:
        report["decision"] = "⚠ 保留旧模型 (L2 排 ST 累计 α 未升)"
        print(f"  → 新模型 L2 未优于旧, 保留旧模型 (candidate: {candidate_path})")

    # === Step 6: 短期 4 周健康度检查 (informational) ===
    backtest_summary = _run_post_evolve_backtest(weeks=4)
    if backtest_summary:
        report["steps"]["backtest_4w"] = backtest_summary

    # 保存进化日志
    _save_evolve_log(report)

    # 推送
    if push:
        _push_report(report)

    return report


def _l2_promotion_check(candidate_path: str, factor_df, report: dict) -> bool:
    """
    P1.1: 用 L2 evaluator 对照新候选 vs 旧生产模型, 决定是否 promote.

    判定指标: 排 ST 累计 α (用户决策, 2026-05-16)
    时间窗: 2024-01-01 起 (固定起点)

    Returns
    -------
    True = 新模型胜, promote; False = 保留旧模型
    """
    from ml.ranker import PRODUCTION_MODEL
    from ml.l2_evaluator import evaluate_model_l2, DEFAULT_START
    import os

    pool_codes = factor_df["code"].tolist() if "code" in factor_df.columns else []
    if not pool_codes:
        logger.warning("L2 promotion check: factor_df 无 code 列, 默认拒 promote")
        report["steps"]["l2_eval"] = {"error": "factor_df 无 code 列"}
        return False

    # 旧模型 L2
    if not os.path.exists(PRODUCTION_MODEL):
        # 首次部署, 无旧模型 → 默认 promote
        logger.info("L2 promotion check: 无旧 PRODUCTION_MODEL, 默认 promote")
        report["steps"]["l2_eval"] = {"first_deploy": True}
        return True

    print(f"  跑旧模型 L2 ({PRODUCTION_MODEL})...")
    try:
        old_l2 = evaluate_model_l2(PRODUCTION_MODEL, pool_codes, start_date=DEFAULT_START)
    except Exception as e:
        logger.warning(f"旧模型 L2 失败: {e}, 默认 promote 新模型")
        report["steps"]["l2_eval"] = {"old_eval_error": str(e)}
        return True

    print(f"  跑新候选 L2 ({candidate_path})...")
    try:
        new_l2 = evaluate_model_l2(candidate_path, pool_codes, start_date=DEFAULT_START)
    except Exception as e:
        logger.error(f"新候选 L2 失败: {e}, 拒 promote 保守")
        report["steps"]["l2_eval"] = {"new_eval_error": str(e)}
        return False

    old_alpha = old_l2["cum_alpha_no_st"]
    new_alpha = new_l2["cum_alpha_no_st"]
    if pd.isna(new_alpha) or pd.isna(old_alpha):
        logger.warning(f"L2 评估有 nan (old={old_alpha} new={new_alpha}), 拒 promote 保守")
        report["steps"]["l2_eval"] = {"nan_result": True,
                                      "old": old_l2, "new": new_l2}
        return False

    delta = new_alpha - old_alpha
    promote = delta > 0
    report["steps"]["l2_eval"] = {
        "old_cum_alpha_no_st": round(old_alpha, 4),
        "new_cum_alpha_no_st": round(new_alpha, 4),
        "delta_pp": round(delta * 100, 2),
        "old_pf_no_st": round(old_l2["pf_no_st"], 2),
        "new_pf_no_st": round(new_l2["pf_no_st"], 2),
        "n_weeks": new_l2["n_weeks"],
        "promote": promote,
    }
    print(f"  L2 排 ST 累计 α: 旧 {old_alpha*100:+.2f}% → 新 {new_alpha*100:+.2f}% "
          f"(Δ {delta*100:+.2f}pp) → {'PROMOTE' if promote else 'KEEP OLD'}")
    return promote


def _run_post_evolve_backtest(weeks: int = 4) -> dict:
    """训练后跑短期回测验证（用当前生产模型，可能是新上线的或保留的旧模型）

    informational only — 不影响 train_model 的上线决策。
    给运维一个"模型在最近实际行情上是否还能跑"的健康度指标。

    Returns
    -------
    dict 或 None: {d_alpha, d_累计, d_n, beat_bench_rate}
    """
    print("\n[5/5] 回测验证（最近 4 周，informational）...")
    try:
        import subprocess
        from datetime import datetime, timedelta
        end = (datetime.now() - timedelta(days=5)).strftime('%Y-%m-%d')
        start = (datetime.now() - timedelta(weeks=weeks + 1)).strftime('%Y-%m-%d')
        out_csv = f"logs/backtest_post_evolve.csv"
        result = subprocess.run(
            ['python3', 'scripts/backtest_year.py',
             '--start', start, '--end', end, '--out', out_csv],
            capture_output=True, text=True, timeout=900,
        )
        if result.returncode != 0:
            logger.warning(f"回测失败: {result.stderr[:300]}")
            return None
        # 解析 D 方案 alpha
        try:
            import pandas as pd
            df = pd.read_csv(out_csv)
            d = df[df["method"] == "D 频次共识(生产)"]
            if d.empty:
                logger.warning("回测无 D 方案观测点，可能数据不足")
                return None
            avg_alpha = float(d["alpha"].mean())
            cum_alpha = float((1 + d["alpha"]).prod() - 1)
            beat = float((d["alpha"] > 0).mean())
            n = len(d)
            print(f"  D 方案 {n} 周: avg_alpha={avg_alpha*100:+.2f}% "
                  f"累计={cum_alpha*100:+.2f}% 跑赢基准率={beat*100:.0f}%")
            if avg_alpha < -0.005:
                logger.warning(f"⚠️ D 方案最近 {n} 周 avg_alpha {avg_alpha*100:+.2f}% 偏低，"
                               f"建议人工 review")
            return {
                "weeks": weeks,
                "n_obs": n,
                "d_avg_alpha": round(avg_alpha, 4),
                "d_cum_alpha": round(cum_alpha, 4),
                "beat_bench_rate": round(beat, 4),
            }
        except Exception as e:
            logger.warning(f"解析回测结果失败: {e}")
            return None
    except subprocess.TimeoutExpired:
        logger.warning("回测超时（>15 分钟）")
        return None
    except Exception as e:
        logger.warning(f"回测异常: {e}")
        return None


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

    decision = report["decision"] or ""
    steps = report["steps"]

    # 按 decision 首字符判断（与 evolve() 主函数输出对齐）
    if decision.startswith("✓"):
        emoji = "✓ 新模型已上线"
    elif decision.startswith("⚠"):
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

Top 5 因子:
{chr(10).join(f'  {i+1}. {f}: {v:.4f}' for i, (f, v) in enumerate(factors.get('top5', [])))}

回测验证（最近 4 周 D 方案）:
{_format_backtest_summary(steps.get('backtest_4w'))}"""

    send_to_all(title, msg)
    print("进化报告已推送到微信")


def _format_backtest_summary(b: dict) -> str:
    """格式化回测验证摘要"""
    if not b:
        return "  (回测失败或样本不足，跳过)"
    return (f"  观测 {b.get('n_obs', 0)} 周  "
            f"avg_alpha={b.get('d_avg_alpha', 0)*100:+.2f}%  "
            f"累计={b.get('d_cum_alpha', 0)*100:+.2f}%  "
            f"跑赢基准={b.get('beat_bench_rate', 0)*100:.0f}%")


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
