"""
优化的 XGBoost 训练模块

改进点:
1. 时间序列交叉验证 (避免未来信息泄露)
2. 正则化 (reg_alpha, reg_lambda)
3. Early stopping
4. 特征相关性检查
5. 训练/验证集分离
"""

import os
import json
import numpy as np
import pandas as pd
from datetime import datetime
from sklearn.model_selection import TimeSeriesSplit
from xgboost import XGBRegressor
import logging

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


def check_feature_correlation(X, threshold=0.95):
    """检查特征相关性，返回高相关特征对"""
    corr = X.corr().abs()
    high_corr = []
    for i in range(len(corr.columns)):
        for j in range(i+1, len(corr.columns)):
            if corr.iloc[i, j] > threshold:
                high_corr.append((corr.columns[i], corr.columns[j], corr.iloc[i, j]))
    return high_corr


def time_series_cv_train(X, y, n_splits=5):
    """时间序列交叉验证"""
    tscv = TimeSeriesSplit(n_splits=n_splits)

    cv_scores = []
    for train_idx, val_idx in tscv.split(X):
        X_train, X_val = X.iloc[train_idx], X.iloc[val_idx]
        y_train, y_val = y.iloc[train_idx], y.iloc[val_idx]

        model = XGBRegressor(
            n_estimators=500,  # 更大，配合 early_stopping
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            reg_alpha=0.1,     # L1 正则
            reg_lambda=1.0,     # L2 正则
            random_state=42,
            verbosity=0,
        )

        model.fit(
            X_train, y_train,
            eval_set=[(X_val, y_val)],
            eval_metric="rmse",
            early_stopping_rounds=20,
            verbose=False
        )

        pred = model.predict(X_val)
        # R² score
        r2 = 1 - np.sum((y_val - pred)**2) / np.sum((y_val - y_val.mean())**2)
        cv_scores.append(r2)

    return cv_scores


def train_model_optimized(train_df: pd.DataFrame) -> dict:
    """
    优化的模型训练函数

    改进:
    - 时间序列CV
    - 正则化
    - Early stopping
    - 特征相关性检查
    """
    os.makedirs(MODEL_DIR, exist_ok=True)

    # 清理数据
    df = train_df.dropna(subset=["label"])
    X = df[FEATURE_COLS].copy()
    y = df["label"].copy()

    # 填充缺失值 (用中位数)
    X = X.fillna(X.median())

    # 移除全 NaN 列 (sentiment_score)
    X = X.dropna(axis=1, how="all")
    actual_features = X.columns.tolist()

    if len(X) < 100:
        logger.warning(f"训练样本不足: {len(X)}")
        return {}

    # 检查特征相关性
    high_corr = check_feature_correlation(X)
    if high_corr:
        logger.info(f"高相关特征对 (>0.95): {len(high_corr)}")
        for f1, f2, val in high_corr[:5]:
            logger.info(f"  {f1} <-> {f2}: {val:.3f}")

    # 时间序列交叉验证
    logger.info("时间序列交叉验证...")
    cv_scores = time_series_cv_train(X, y, n_splits=5)

    # 全量训练 (最佳 iteration)
    model = XGBRegressor(
        n_estimators=500,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        verbosity=0,
    )

    # 使用 early stopping (留出 20% 作为验证集)
    split = int(len(X) * 0.8)
    model.fit(
        X.iloc[:split], y.iloc[:split],
        eval_set=[(X.iloc[split:], y.iloc[split:])],
        eval_metric="rmse",
        early_stopping_rounds=20,
        verbose=False
    )

    # 特征重要性
    importance = dict(zip(actual_features, [float(x) for x in model.feature_importances_]))
    importance = {k: round(v, 4) for k, v in sorted(importance.items(), key=lambda x: -x[1])}

    new_r2 = round(float(np.mean(cv_scores)), 4)
    new_std = round(float(np.std(cv_scores)), 4)

    # 版本管理
    PRODUCTION_MODEL = os.path.join(MODEL_DIR, "xgb_ranker.json")
    IMPORTANCE_PATH = os.path.join(MODEL_DIR, "feature_importance.json")
    HISTORY_PATH = os.path.join(MODEL_DIR, "model_history.json")

    is_new_best = True
    old_r2 = None

    if os.path.exists(PRODUCTION_MODEL):
        try:
            with open(HISTORY_PATH, "r") as f:
                history = json.load(f)
            old_r2 = history.get("current", {}).get("cv_r2_mean")
            if old_r2 is not None and new_r2 < old_r2:
                is_new_best = False
                logger.info(f"新模型 R²={new_r2:.4f} < 旧模型 R²={old_r2:.4f}，保留旧模型")
        except:
            pass

    if is_new_best:
        # 备份旧模型
        if os.path.exists(PRODUCTION_MODEL):
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = os.path.join(MODEL_DIR, f"xgb_ranker_{timestamp}.json")
            import shutil
            shutil.copy2(PRODUCTION_MODEL, backup_path)

        model.save_model(PRODUCTION_MODEL)

        with open(IMPORTANCE_PATH, "w") as f:
            json.dump(importance, f, indent=2, ensure_ascii=False)

        # 更新历史
        try:
            with open(HISTORY_PATH, "r") as f:
                history = json.load(f)
        except:
            history = {"versions": [], "current": {}}

        version = {
            "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "cv_r2_mean": new_r2,
            "cv_r2_std": new_std,
            "train_samples": len(X),
            "features": actual_features,
            "top_factors": list(importance.keys())[:5],
        }
        history["versions"].append(version)
        history["versions"] = history["versions"][-20:]
        history["current"] = version

        with open(HISTORY_PATH, "w") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

        logger.info(f"新模型已上线: R²={new_r2:.4f}±{new_std:.4f}, samples={len(X)}")

    return {
        "model_path": PRODUCTION_MODEL if is_new_best else "candidate",
        "train_samples": len(X),
        "cv_r2_mean": new_r2,
        "cv_r2_std": new_std,
        "feature_importance": importance,
        "is_new_best": is_new_best,
        "old_r2": old_r2,
        "features_used": actual_features,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print("优化的训练模块已就绪")
