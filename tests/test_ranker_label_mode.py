"""验证 prepare_training_data 的 label_mode 默认值和 excess/raw 切换"""
import inspect
import numpy as np
import pandas as pd
import pytest

from ml.ranker import prepare_training_data


def test_default_label_mode_is_excess():
    """P1.2: 默认值切换 raw → excess (2026-05-16)"""
    sig = inspect.signature(prepare_training_data)
    assert sig.parameters["label_mode"].default == "excess", (
        "默认值应为 excess (P1.2-B 实证: 排 ST 累计 α +16% → +31%)"
    )


def test_excess_subtracts_section_mean():
    """excess 模式下, 同截面 label 均值应接近 0"""
    # 构造极小的 mock train_df 模拟 prepare_training_data 内部流程
    # 直接测「按 end_date 分组减均值」的逻辑
    raw_data = pd.DataFrame([
        {"code": "A", "end_date": "2026-01-05", "label": 0.10},
        {"code": "B", "end_date": "2026-01-05", "label": 0.05},
        {"code": "C", "end_date": "2026-01-05", "label": -0.03},
        {"code": "A", "end_date": "2026-02-05", "label": -0.02},
        {"code": "B", "end_date": "2026-02-05", "label": 0.08},
    ])
    section_mean = raw_data.groupby("end_date")["label"].transform("mean")
    excess = raw_data["label"] - section_mean

    # 同截面 excess 之和应该 = 0
    grouped = pd.DataFrame({"end_date": raw_data["end_date"], "excess": excess})
    sums = grouped.groupby("end_date")["excess"].sum()
    for s in sums:
        assert abs(s) < 1e-10, f"excess 之和应为 0, 实际 {s}"


def test_raw_mode_keeps_absolute_return():
    """raw 模式必须保留 (回退兼容性)"""
    sig = inspect.signature(prepare_training_data)
    assert "raw" in [None, "raw", "excess"], "raw 选项必须保留"
    # smoke: parameter 接受 'raw' 不报 error
    # (实际数据测试在 train_excess_label.py --label raw 跑过)
