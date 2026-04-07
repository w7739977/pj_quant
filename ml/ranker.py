"""
ML 选股模型 - XGBoost 排名

训练流程:
1. 用因子作为特征
2. 用未来 N 日收益率作为标签
3. 训练 XGBoost 回归模型
4. 预测打分，选出预期收益最高的股票

使用方式:
  python main.py train     # 训练/更新模型
  python main.py predict   # 用模型预测今日选股
"""

import os
import json
import shutil
import pandas as pd
import numpy as np
import logging
from datetime import datetime, timedelta

from factors.calculator import compute_stock_pool_factors
from factors.data_loader import get_stock_daily

logger = logging.getLogger(__name__)

MODEL_DIR = "ml/models"
FEATURE_COLS = [
    "mom_5d", "mom_10d", "mom_20d", "mom_60d",
    "vol_10d", "vol_20d",
    "avg_turnover_5d", "avg_turnover_20d", "turnover_accel",
    "vol_price_diverge", "volume_surge",
    "ma5_bias", "ma10_bias", "ma20_bias", "rsi_14",
    "pe_ttm", "pb", "turnover_rate", "volume_ratio",
    "sentiment_score",
]

# 特征重要性保存路径
IMPORTANCE_PATH = os.path.join(MODEL_DIR, "feature_importance.json")
# 模型版本历史
HISTORY_PATH = os.path.join(MODEL_DIR, "model_history.json")
# 当前生产模型路径
PRODUCTION_MODEL = os.path.join(MODEL_DIR, "xgb_ranker.json")


def prepare_training_data(
    factor_df: pd.DataFrame,
    forward_days: int = 20,
    end_date: str = None,
) -> pd.DataFrame:
    """
    准备训练数据: 基于本地缓存的滚动截面生成 (因子 + 未来N日收益率)

    不依赖实时网络，全部从本地 SQLite 读取。
    使用历史滚动窗口生成多个截面样本。
    **修复**: 基本面因子从 SQLite 读取实际值，不再用 NaN 占位。

    Parameters
    ----------
    factor_df : DataFrame  当日因子矩阵（用于确定股票池）
    forward_days : int  前瞻天数
    end_date : str  未使用，保留接口兼容

    Returns
    -------
    DataFrame: 含 feature_cols + label 列
    """
    from data.storage import load_stock_daily
    from factors.calculator import (
        calc_momentum, calc_volatility, calc_turnover_factor,
        calc_volume_price, calc_technical,
    )

    symbols = factor_df["code"].tolist()
    total = len(symbols)
    records = []

    for i, sym in enumerate(symbols):
        try:
            df = load_stock_daily(sym)
            if df.empty or len(df) < 120:
                continue

            # 滚动截面：每 20 天取一个样本
            for end_idx in range(60, len(df) - forward_days, 20):
                window = df.iloc[:end_idx + 1]
                fwd = df.iloc[end_idx:end_idx + forward_days + 1]
                if len(fwd) < forward_days + 1:
                    continue

                forward_return = float(fwd.iloc[-1]["close"]) / float(fwd.iloc[0]["close"]) - 1.0

                # 计算该截面的技术因子
                factors = {"code": sym, "label": forward_return}
                factors.update(calc_momentum(window))
                factors.update(calc_volatility(window))
                factors.update(calc_turnover_factor(window))
                factors.update(calc_volume_price(window))
                factors.update(calc_technical(window))

                # **修复**: 从 window 最后一行获取基本面因子（当日值）
                last_row = window.iloc[-1]
                factors["pe_ttm"] = last_row.get("pe_ttm", np.nan)
                factors["pb"] = last_row.get("pb", np.nan)
                factors["turnover_rate"] = last_row.get("turnover_rate", np.nan)
                factors["volume_ratio"] = last_row.get("volume_ratio", np.nan)

                # 情绪因子暂用 NaN（需实时调用）
                factors["sentiment_score"] = np.nan

                records.append(factors)
        except Exception:
            continue

        if (i + 1) % 200 == 0:
            logger.info(f"  准备训练数据: {i+1}/{total} (已生成 {len(records)} 条)")

    train_df = pd.DataFrame(records)
    logger.info(f"  训练样本生成完成: {len(train_df)} 条 ({len(symbols)} 只股票)")

    # 打印基本面因子使用率
    fund_cols = ["pe_ttm", "pb", "turnover_rate", "volume_ratio"]
    for col in fund_cols:
        non_null = train_df[col].notna().sum()
        logger.info(f"    {col}: {non_null}/{len(train_df)} ({non_null*100/len(train_df):.1f}%) 有数据")

    return train_df


def train_model(train_df: pd.DataFrame) -> dict:
    """
    训练 XGBoost 排名模型（带版本管理）

    Returns
    -------
    dict: {model_path, metrics, feature_importance, is_new_best}
    """
    from xgboost import XGBRegressor
    from sklearn.model_selection import cross_val_score

    os.makedirs(MODEL_DIR, exist_ok=True)

    # 清理数据
    df = train_df.dropna(subset=["label"])
    X = df[FEATURE_COLS].copy()
    y = df["label"].copy()

    # 填充缺失值
    X = X.fillna(X.median())

    if len(X) < 30:
        logger.warning(f"训练样本不足: {len(X)}")
        return {}

    # 训练
    model = XGBRegressor(
        n_estimators=200,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=42,
        verbosity=0,
    )

    # 交叉验证
    cv_scores = cross_val_score(model, X, y, cv=min(5, len(X) // 10), scoring="r2")

    # 全量训练
    model.fit(X, y)

    # 特征重要性
    importance = dict(zip(FEATURE_COLS, [float(x) for x in model.feature_importances_]))
    importance = {k: round(v, 4) for k, v in sorted(importance.items(), key=lambda x: -x[1])}

    new_r2 = round(float(cv_scores.mean()), 4)

    # === 版本管理：对比新旧模型 ===
    is_new_best = True
    old_r2 = None

    if os.path.exists(PRODUCTION_MODEL):
        history = _load_history()
        old_r2 = history.get("current", {}).get("cv_r2_mean")
        if old_r2 is not None and new_r2 < old_r2:
            is_new_best = False
            logger.info(f"新模型 R²={new_r2:.4f} < 旧模型 R²={old_r2:.4f}，保留旧模型")

    if is_new_best:
        # 备份旧模型
        if os.path.exists(PRODUCTION_MODEL):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(MODEL_DIR, f"xgb_ranker_{timestamp}.json")
            shutil.copy2(PRODUCTION_MODEL, backup_path)

        # 保存新模型为生产模型
        model.save_model(PRODUCTION_MODEL)

        with open(IMPORTANCE_PATH, "w") as f:
            json.dump(importance, f, indent=2, ensure_ascii=False)

        # 更新历史记录
        _save_history(new_r2, float(cv_scores.std()), len(X), importance)

        logger.info(f"新模型已上线: R²={new_r2:.4f}, samples={len(X)}")
    else:
        # 仍然保存新模型作为候选，但不替换生产模型
        candidate_path = os.path.join(MODEL_DIR, "xgb_ranker_candidate.json")
        model.save_model(candidate_path)
        logger.info(f"新模型已保存为候选: {candidate_path}")

    result = {
        "model_path": PRODUCTION_MODEL if is_new_best else candidate_path,
        "train_samples": len(X),
        "cv_r2_mean": new_r2,
        "cv_r2_std": round(float(cv_scores.std()), 4),
        "feature_importance": importance,
        "is_new_best": is_new_best,
        "old_r2": old_r2,
    }

    return result


def _load_history() -> dict:
    """加载模型版本历史"""
    if not os.path.exists(HISTORY_PATH):
        return {"versions": [], "current": {}}
    try:
        with open(HISTORY_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {"versions": [], "current": {}}


def _save_history(r2_mean: float, r2_std: float, samples: int, importance: dict):
    """保存模型版本记录"""
    history = _load_history()

    version = {
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "cv_r2_mean": r2_mean,
        "cv_r2_std": r2_std,
        "train_samples": samples,
        "top_factors": list(importance.keys())[:5],
    }

    history["versions"].append(version)
    # 只保留最近 20 个版本
    history["versions"] = history["versions"][-20:]
    history["current"] = version

    with open(HISTORY_PATH, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def get_model_info() -> dict:
    """获取当前生产模型信息"""
    info = {
        "has_model": os.path.exists(PRODUCTION_MODEL),
        "model_path": PRODUCTION_MODEL,
    }

    history = _load_history()
    info["current"] = history.get("current", {})
    info["version_count"] = len(history.get("versions", []))

    # 读取特征重要性
    if os.path.exists(IMPORTANCE_PATH):
        with open(IMPORTANCE_PATH, "r") as f:
            info["feature_importance"] = json.load(f)

    return info


def predict(factor_df: pd.DataFrame) -> pd.DataFrame:
    """
    用训练好的模型预测股票得分

    Returns
    -------
    DataFrame: [code, predicted_return, rank]
    """
    from xgboost import XGBRegressor

    model_path = os.path.join(MODEL_DIR, "xgb_ranker.json")
    if not os.path.exists(model_path):
        logger.warning("模型不存在，请先训练: python main.py train")
        return pd.DataFrame()

    model = XGBRegressor()
    model.load_model(model_path)

    X = factor_df[FEATURE_COLS].copy()
    X = X.fillna(X.median())

    preds = model.predict(X)

    result = factor_df[["code"]].copy()
    result["predicted_return"] = preds
    result["rank"] = result["predicted_return"].rank(ascending=False).astype(int)
    result = result.sort_values("rank")

    return result.reset_index(drop=True)
