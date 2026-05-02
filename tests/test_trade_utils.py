"""
测试 portfolio/trade_utils.py 中的交易工具函数
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from portfolio.trade_utils import (
    is_tradeable,
    calc_shares,
    estimate_buy_cost,
    estimate_sell_cost,
)


# ============ is_tradeable ============

class TestIsTradeable:
    """板块过滤测试"""

    # 主板 + 创业板 应通过
    def test_mainboard_000(self):
        assert is_tradeable("000001") is True  # 平安银行

    def test_mainboard_600(self):
        assert is_tradeable("600519") is True  # 贵州茅台

    def test_mainboard_601(self):
        assert is_tradeable("601398") is True  # 工商银行

    def test_mainboard_603(self):
        assert is_tradeable("603259") is True

    def test_mainboard_605(self):
        assert is_tradeable("605001") is True

    def test_gem_300(self):
        assert is_tradeable("300750") is True  # 宁德时代

    def test_sme_002(self):
        assert is_tradeable("002230") is True

    def test_sme_003(self):
        assert is_tradeable("003816") is True

    # 科创板 应允许（50 万本金已达开户门槛）
    def test_star_688(self):
        assert is_tradeable("688001") is True

    def test_star_688_upper(self):
        assert is_tradeable("688599") is True

    # 北交所 已禁止（2026-05-02 起从池子移除：50 万门槛 + 历史回测表现差）
    def test_bse_83_blocked(self):
        assert is_tradeable("830799") is False

    def test_bse_43_blocked(self):
        assert is_tradeable("430047") is False

    def test_bse_87_blocked(self):
        assert is_tradeable("870001") is False

    def test_bse_88_blocked(self):
        assert is_tradeable("880023") is False

    # 北交所 2024 改版新代码段 (920) 也禁止
    def test_bse_920_blocked(self):
        assert is_tradeable("920001") is False

    def test_bse_920_new(self):
        assert is_tradeable("920857") is False

    # 创业板新代码段 (301/302) 应允许
    def test_gem_301(self):
        assert is_tradeable("301001") is True

    def test_gem_302(self):
        assert is_tradeable("302001") is True

    # 科创板新代码段 (689) 应允许
    def test_star_689(self):
        assert is_tradeable("689001") is True

    # B股 应拒绝（需外汇账户）
    def test_b_sh_900(self):
        assert is_tradeable("900901") is False

    def test_b_sz_200(self):
        assert is_tradeable("200002") is False

    # 边界 / 非法输入
    def test_empty(self):
        assert is_tradeable("") is False

    def test_short(self):
        assert is_tradeable("12345") is False

    def test_leading_zero(self):
        assert is_tradeable("0000001") is False


# ============ calc_shares ============

class TestCalcShares:
    """100股整手计算"""

    def test_normal(self):
        result = calc_shares(10000, 5.5)
        assert result["shares"] == 1800
        assert result["amount"] == 9900.0
        assert result["remaining"] == 100.0

    def test_insufficient(self):
        """499元不够买1手(100×5=500)"""
        result = calc_shares(499, 5.0)
        assert result["shares"] == 0
        assert result["amount"] == 0.0
        assert result["remaining"] == 499.0

    def test_exact_one_lot(self):
        """刚好够1手"""
        result = calc_shares(500, 5.0)
        assert result["shares"] == 100
        assert result["amount"] == 500.0
        assert result["remaining"] == 0.0

    def test_zero_price(self):
        result = calc_shares(10000, 0)
        assert result["shares"] == 0

    def test_zero_capital(self):
        result = calc_shares(0, 5.0)
        assert result["shares"] == 0

    def test_negative_capital(self):
        result = calc_shares(-100, 5.0)
        assert result["shares"] == 0


# ============ estimate_buy_cost / estimate_sell_cost ============

class TestCostEstimation:
    """交易成本估算"""

    def test_buy_small_triggers_min_commission(self):
        """小额买入触发最低佣金5元"""
        amount = 1000.0
        cost = estimate_buy_cost(amount)
        # commission = max(1000*0.00025, 5) = 5.0
        # transfer = 1000 * 0.00001 = 0.01
        assert cost == 5.01

    def test_buy_large(self):
        """大额买入按费率计算"""
        amount = 100000.0
        cost = estimate_buy_cost(amount)
        # commission = 100000*0.00025 = 25
        # transfer = 100000*0.00001 = 1
        assert cost == 26.0

    def test_sell_includes_stamp_tax(self):
        """卖出包含印花税"""
        amount = 10000.0
        cost = estimate_sell_cost(amount)
        # commission = max(10000*0.00025, 5) = 5
        # stamp = 10000*0.001 = 10
        # transfer = 10000*0.00001 = 0.1
        assert cost == 15.1

    def test_buy_no_stamp_tax(self):
        """买入不含印花税（对比卖出）"""
        amount = 10000.0
        buy_cost = estimate_buy_cost(amount)
        sell_cost = estimate_sell_cost(amount)
        # 卖出成本 > 买入成本（因为印花税）
        assert sell_cost > buy_cost
