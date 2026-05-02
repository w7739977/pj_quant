"""测试财务因子接入"""
import pytest
from unittest.mock import patch, MagicMock
import pandas as pd


def test_save_and_load_pit():
    """测试 save_batch + get_latest_pit"""
    from data.financial_indicator import save_batch, get_latest_pit

    save_batch([{
        "code": "TEST_001", "ann_date": "20240430", "end_date": "20240331",
        "roe_yearly": 12.5, "or_yoy": 8.3,
        "dt_eps_yoy": 5.0, "debt_to_assets": 45.0,
    }])

    # PIT 查询：截面 2024-05-01 应能查到 4-30 公告的数据
    result = get_latest_pit("TEST_001", "20240501")
    assert result["roe_yearly"] == 12.5
    assert result["or_yoy"] == 8.3

    # 截面 2024-04-29 应查不到（公告日是 4-30，未来数据）
    result = get_latest_pit("TEST_001", "20240429")
    assert result == {}


def test_pit_takes_latest():
    """同一股票多份公告，取最近的"""
    from data.financial_indicator import save_batch, get_latest_pit

    save_batch([
        {"code": "TEST_002", "ann_date": "20240430", "end_date": "20240331",
         "roe_yearly": 10.0},
        {"code": "TEST_002", "ann_date": "20240825", "end_date": "20240630",
         "roe_yearly": 12.0},
    ])

    # 截面 2024-09-01 应取 8-25 那一份
    result = get_latest_pit("TEST_002", "20240901")
    assert result["roe_yearly"] == 12.0


def test_lookup_with_cache():
    """测试 _FIN_CACHE 全局缓存的 PIT 查询（二分查找）"""
    from ml.ranker import _lookup_financial_pit
    import ml.ranker as ranker_mod

    # 注入测试缓存（code → [(ann_date, factors), ...] 格式）
    ranker_mod._FIN_CACHE = {
        "000001": [
            ("20240430", {"roe_yearly": 5.0, "or_yoy": 3.0,
                         "dt_eps_yoy": 2.0, "debt_to_assets": 90.0}),
            ("20240825", {"roe_yearly": 6.0, "or_yoy": 4.0,
                         "dt_eps_yoy": 3.0, "debt_to_assets": 89.0}),
        ]
    }
    result = _lookup_financial_pit("000001", "20240901")
    assert result["roe_yearly"] == 6.0
    result_early = _lookup_financial_pit("000001", "20240501")
    assert result_early["roe_yearly"] == 5.0
    result_too_early = _lookup_financial_pit("000001", "20240101")
    assert result_too_early == {}
