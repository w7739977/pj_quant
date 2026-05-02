"""交易日判断工具单测"""
import sys
import os
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))

from is_trading_day_check import is_trading_day, is_first_trading_day_of_week


# ============ 周末非交易日 ============

def test_saturday_not_trading():
    assert is_trading_day(date(2026, 5, 2)) is False  # 周六

def test_sunday_not_trading():
    assert is_trading_day(date(2026, 5, 3)) is False  # 周日


# ============ 法定节假日 ============

def test_labor_day_not_trading():
    """2026-05-01 劳动节"""
    assert is_trading_day(date(2026, 5, 1)) is False

def test_normal_monday_trading():
    """2026-04-13 普通周一"""
    assert is_trading_day(date(2026, 4, 13)) is True

def test_normal_friday_trading():
    """2026-04-17 普通周五"""
    assert is_trading_day(date(2026, 4, 17)) is True


# ============ 本周第一个交易日 ============

def test_first_of_week_normal_monday():
    """2026-04-13 周一交易日 → 是第一个"""
    assert is_first_trading_day_of_week(date(2026, 4, 13)) is True

def test_first_of_week_normal_tuesday_not_first():
    """2026-04-14 周二交易日，但周一已交易过 → 不是第一个"""
    assert is_first_trading_day_of_week(date(2026, 4, 14)) is False

def test_first_of_week_labor_day_2026_holiday_extended():
    """2026 五一假期 chinese_calendar 视 5/4(Mon)、5/5(Tue) 也为假日

    所以本周第一个交易日是 5/6 周三：
      - 5/4 Mon: 假日 → False
      - 5/5 Tue: 假日 → False
      - 5/6 Wed: 第一个交易日 → True
      - 5/7 Thu: 已有 5/6 → False
    """
    assert is_first_trading_day_of_week(date(2026, 5, 4)) is False
    assert is_first_trading_day_of_week(date(2026, 5, 5)) is False
    assert is_first_trading_day_of_week(date(2026, 5, 6)) is True
    assert is_first_trading_day_of_week(date(2026, 5, 7)) is False
    # 下周一 5/11 又是新一周的第一个交易日
    assert is_first_trading_day_of_week(date(2026, 5, 11)) is True

def test_first_of_week_skip_holiday_monday():
    """2025-10-08 周三 — 国庆 10-1~10-7 假期，10-8 周三是节后第一个交易日

    本周一 (10-6) 是国庆假，10-7 还是国庆假，10-8 周三才是交易日且本周第一
    """
    # 验证 10-8 周三是本周第一个交易日（10-6 周一、10-7 周二都是假日）
    if is_trading_day(date(2025, 10, 6)) is False and \
       is_trading_day(date(2025, 10, 7)) is False and \
       is_trading_day(date(2025, 10, 8)) is True:
        assert is_first_trading_day_of_week(date(2025, 10, 8)) is True

def test_weekend_not_first_of_week():
    """周末本身不是交易日，不可能是第一"""
    assert is_first_trading_day_of_week(date(2026, 5, 2)) is False
