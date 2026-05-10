"""scripts.track_picks_performance 核心 helper 单测"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from track_picks_performance import (  # noqa: E402
    consensus_picks_for, fwd_return, benchmark_5d,
)


# ============ fwd_return ============

def _make_df(dates, closes):
    return pd.DataFrame({
        "date_str": dates,
        "close": closes,
    })


def test_fwd_return_basic():
    """简单 5d 收益: 10 → 15 = +50%"""
    df = _make_df(
        ["2026-04-25", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-06", "2026-05-07"],
        [10, 11, 12, 13, 14, 15],
    )
    sd = {"000001": df}
    ret = fwd_return(sd, "000001", "2026-04-25", hold=5)
    assert ret == pytest.approx(0.5, abs=1e-9)


def test_fwd_return_no_code():
    sd = {}
    assert fwd_return(sd, "999999", "2026-04-25") is None


def test_fwd_return_short_after():
    """after 不足 hold → None"""
    df = _make_df(["2026-04-25", "2026-04-28"], [10, 11])
    sd = {"000001": df}
    assert fwd_return(sd, "000001", "2026-04-25", hold=5) is None


def test_fwd_return_no_before():
    """D 之前没数据 → None"""
    df = _make_df(["2026-04-28", "2026-04-29", "2026-04-30", "2026-05-06", "2026-05-07", "2026-05-08"],
                  [11, 12, 13, 14, 15, 16])
    sd = {"000001": df}
    # D=2026-04-25 之前没有 bar
    assert fwd_return(sd, "000001", "2026-04-25", hold=5) is None


def test_fwd_return_p0_zero():
    """p0=0 时返回 None 防除零"""
    df = _make_df(
        ["2026-04-25", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-06", "2026-05-07"],
        [0.0, 11, 12, 13, 14, 15],
    )
    sd = {"000001": df}
    assert fwd_return(sd, "000001", "2026-04-25", hold=5) is None


# ============ consensus_picks_for ============

def _scored(rows):
    return pd.DataFrame(rows)


def test_consensus_picks_short_buffer():
    """idx < window → 空"""
    buffer = ["2026-01-05"]
    daily = {"2026-01-05": _scored([{"code": "A", "final_score": 1.0}])}
    picks = consensus_picks_for("2026-01-05", daily, buffer, window=5)
    assert picks == []


def test_consensus_picks_d_not_in_buffer():
    buffer = ["2026-01-01", "2026-01-02"]
    daily = {}
    picks = consensus_picks_for("2026-01-99", daily, buffer)
    assert picks == []


def test_consensus_picks_freq_priority():
    """A 出现 3/3 天，freq=3；B 出现 1 次但分高，freq=1 → A 排第一"""
    buffer = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06"]
    daily = {
        "2026-01-01": _scored([{"code": "A", "final_score": 0.5}, {"code": "B", "final_score": 0.1}]),
        "2026-01-02": _scored([{"code": "A", "final_score": 0.4}]),
        "2026-01-03": _scored([{"code": "A", "final_score": 0.3}, {"code": "B", "final_score": 99.0}]),
    }
    picks = consensus_picks_for("2026-01-06", daily, buffer, top_n=2, window=3)
    codes = [p["code"] for p in picks]
    assert codes[0] == "A"
    assert codes[0] == "A" and picks[0]["freq"] == 3
    assert "B" in codes


def test_consensus_picks_tiebreaker_by_avg_score():
    """同频次时按 avg_score 排"""
    buffer = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06"]
    daily = {
        "2026-01-01": _scored([{"code": "LOW", "final_score": 0.1}, {"code": "HI", "final_score": 1.0}]),
        "2026-01-02": _scored([{"code": "LOW", "final_score": 0.1}, {"code": "HI", "final_score": 1.0}]),
        "2026-01-03": _scored([{"code": "LOW", "final_score": 0.1}, {"code": "HI", "final_score": 1.0}]),
    }
    picks = consensus_picks_for("2026-01-06", daily, buffer, top_n=2, window=3)
    assert picks[0]["code"] == "HI"
    assert picks[1]["code"] == "LOW"
    assert picks[0]["freq"] == picks[1]["freq"] == 3


def test_consensus_picks_top_n_limit():
    """top_n 截取"""
    buffer = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06"]
    daily = {
        "2026-01-01": _scored([{"code": c, "final_score": float(i)} for i, c in enumerate("ABCDE")]),
        "2026-01-02": _scored([{"code": c, "final_score": float(i)} for i, c in enumerate("ABCDE")]),
        "2026-01-03": _scored([{"code": c, "final_score": float(i)} for i, c in enumerate("ABCDE")]),
    }
    picks = consensus_picks_for("2026-01-06", daily, buffer, top_n=3, window=3)
    assert len(picks) == 3


def test_consensus_picks_per_day_top_n_filter():
    """consensus_picks_for 内部对每日只看 top_n=3 (default 内部)，
    分排第 4 的股票不计 freq"""
    buffer = ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06"]
    # 每天 5 只，但默认 top_n=10 全计入 (default)
    # 用 top_n=3 让排第 4 的不被计入
    daily = {
        "2026-01-01": _scored([{"code": c, "final_score": s}
                              for c, s in [("A", 5), ("B", 4), ("C", 3), ("D", 2), ("E", 1)]]),
        "2026-01-02": _scored([{"code": c, "final_score": s}
                              for c, s in [("A", 5), ("B", 4), ("C", 3), ("D", 2), ("E", 1)]]),
        "2026-01-03": _scored([{"code": c, "final_score": s}
                              for c, s in [("A", 5), ("B", 4), ("C", 3), ("D", 2), ("E", 1)]]),
    }
    picks = consensus_picks_for("2026-01-06", daily, buffer, top_n=3, window=3)
    # top_n=3 内部既限制每日 daily top 也限制最终结果，D/E 不在前 3 → 不进 freq
    codes = [p["code"] for p in picks]
    assert codes == ["A", "B", "C"]
    assert all(p["freq"] == 3 for p in picks)


# ============ benchmark_5d ============

def test_benchmark_5d_average():
    """三只股票 5d 收益 [+10%, 0%, -10%] → mean 0%"""
    sd = {
        "001": _make_df(
            ["2026-04-25", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-06", "2026-05-07"],
            [10, 10, 10, 10, 10, 11],
        ),
        "002": _make_df(
            ["2026-04-25", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-06", "2026-05-07"],
            [10, 10, 10, 10, 10, 10],
        ),
        "003": _make_df(
            ["2026-04-25", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-06", "2026-05-07"],
            [10, 10, 10, 10, 10, 9],
        ),
    }
    bench = benchmark_5d(sd, "2026-04-25")
    assert bench == pytest.approx(0.0, abs=1e-9)


def test_benchmark_5d_skips_none():
    """部分股票数据不足，仅平均有数据的"""
    sd = {
        "001": _make_df(
            ["2026-04-25", "2026-04-28", "2026-04-29", "2026-04-30", "2026-05-06", "2026-05-07"],
            [10, 10, 10, 10, 10, 11],
        ),
        "002": _make_df(["2026-04-25"], [10]),  # 数据不足
    }
    bench = benchmark_5d(sd, "2026-04-25")
    assert bench == pytest.approx(0.1, abs=1e-9)
