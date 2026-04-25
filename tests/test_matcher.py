"""涨跌停判断测试"""
from simulation.matcher import _check_limit


def test_check_limit_mainboard():
    """主板 10% 涨跌停"""
    assert _check_limit("600519", 11.0, 10.0) == (True, False)
    assert _check_limit("600519", 9.0, 10.0) == (False, True)
    assert _check_limit("000001", 11.0, 10.0) == (True, False)
    assert _check_limit("000001", 9.0, 10.0) == (False, True)
    assert _check_limit("600519", 10.5, 10.0) == (False, False)


def test_check_limit_chinext():
    """创业板 300xxx 20% 涨跌停"""
    # 未涨停
    assert _check_limit("300001", 11.0, 10.0) == (False, False)
    assert _check_limit("300001", 11.5, 10.0) == (False, False)
    # 涨停 +20%
    assert _check_limit("300001", 12.0, 10.0)[0] is True
    # 跌停 -20%
    assert _check_limit("300001", 8.0, 10.0)[1] is True
    # 15% 不算涨停
    assert _check_limit("300001", 11.5, 10.0) == (False, False)


def test_check_limit_star():
    """科创板 688xxx 20% 涨跌停"""
    assert _check_limit("688001", 12.0, 10.0)[0] is True
    assert _check_limit("688001", 8.0, 10.0)[1] is True
    assert _check_limit("688001", 11.0, 10.0) == (False, False)


def test_check_limit_bse():
    """北交所 8xx/4xx 30% 涨跌停"""
    assert _check_limit("830001", 13.0, 10.0)[0] is True
    assert _check_limit("830001", 7.0, 10.0)[1] is True
    assert _check_limit("430001", 13.0, 10.0)[0] is True
    assert _check_limit("830001", 12.0, 10.0) == (False, False)


def test_check_limit_edge():
    """边界: prev_close=0 或 price=0"""
    assert _check_limit("000001", 11.0, 0) == (False, False)
    assert _check_limit("000001", 0, 10.0) == (False, False)
