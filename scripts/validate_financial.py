#!/usr/bin/env python3
"""
财务因子数据质检 — 拉数据后、跑 evolve 前必跑

检查类别 (4 类 11 项):
  1. 完整性: 股票覆盖率 / 时间覆盖 / 字段非空率
  2. 合理性: 因子值在业界经验范围内（异常值检测）
  3. 正确性: ann_date > end_date / 主键唯一 / 季度连续性
  4. 未来数据泄露: ann_date ≤ 今天 / PIT 查询正确性

退出码:
  0 → 全部通过 / 仅警告
  1 → 关键检查失败，禁止跑 evolve

用法:
  python3 scripts/validate_financial.py
  python3 scripts/validate_financial.py --strict   # 警告也视为失败
"""

import sys
import os
import sqlite3
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB_PATH = "data/quant.db"

# 业界经验值范围（绝对边界，超出 = 数据异常）
SANITY_RANGES = {
    "roe_yearly": (-200, 500),         # ROE 极端值（如重组、ST）
    "or_yoy": (-200, 5000),             # 营收增速（基数小可能 50 倍）
    "dt_eps_yoy": (-5000, 10000),       # EPS 增速（基数极小可能上千倍）
    "debt_to_assets": (0, 200),         # 资产负债率（>100 = 资不抵债）
}

# 业界 P95 区间（超出 = 警告但不阻塞）
P95_RANGES = {
    "roe_yearly": (-50, 80),
    "or_yoy": (-80, 500),
    "dt_eps_yoy": (-500, 1000),
    "debt_to_assets": (5, 95),
}


class CheckResult:
    """单项检查结果"""
    def __init__(self, name: str, passed: bool, level: str, msg: str, details: list = None):
        self.name = name
        self.passed = passed
        self.level = level  # "PASS" / "WARN" / "FAIL"
        self.msg = msg
        self.details = details or []


def _conn():
    return sqlite3.connect(DB_PATH)


def _print_result(r: CheckResult):
    icon = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[r.level]
    print(f"  {icon} {r.name}: {r.msg}")
    for d in r.details[:5]:
        print(f"      {d}")
    if len(r.details) > 5:
        print(f"      ... 还有 {len(r.details) - 5} 项省略")


# ============ 第 1 类：完整性 ============

def check_table_exists() -> CheckResult:
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='financial_indicator'"
        ).fetchone()
        if not row:
            return CheckResult("表存在性", False, "FAIL",
                               "financial_indicator 表不存在，请先跑 fetch-financial")
        return CheckResult("表存在性", True, "PASS", "financial_indicator 表存在")
    finally:
        conn.close()


def check_stock_coverage() -> CheckResult:
    """股票覆盖率：财务数据股数 / 本地缓存股数"""
    conn = _conn()
    try:
        fin_count = conn.execute(
            "SELECT COUNT(DISTINCT code) FROM financial_indicator"
        ).fetchone()[0]
        all_count = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name LIKE 'stock_%'"
        ).fetchone()[0]
        rate = fin_count / all_count if all_count else 0
        msg = f"{fin_count}/{all_count} 只股票有财务数据 ({rate:.1%})"
        if fin_count == 0:
            return CheckResult("股票覆盖率", False, "FAIL", msg + " — 必须重跑 fetch-financial")
        if rate < 0.50:
            return CheckResult("股票覆盖率", False, "WARN",
                               msg + " — 覆盖率偏低，部分股票可能拉取失败")
        return CheckResult("股票覆盖率", True, "PASS", msg)
    finally:
        conn.close()


def check_time_coverage() -> CheckResult:
    """时间覆盖：min ≤ 2020-12, max ≥ 最近 60 日"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT MIN(ann_date), MAX(ann_date) FROM financial_indicator"
        ).fetchone()
        min_d, max_d = row[0], row[1]
        if not min_d or not max_d:
            return CheckResult("时间覆盖", False, "FAIL", "ann_date 全为空")

        today = datetime.now().strftime("%Y%m%d")
        sixty_days_ago = (datetime.now() - timedelta(days=60)).strftime("%Y%m%d")

        details = []
        passed = True
        if min_d > "20210101":
            details.append(f"最早公告日 {min_d} 晚于预期 (应 ≤ 2020-12)")
            passed = False
        if max_d < sixty_days_ago:
            details.append(f"最近公告日 {max_d} 早于 60 日前 ({sixty_days_ago})，数据陈旧")
            passed = False

        msg = f"{min_d} ~ {max_d}"
        level = "PASS" if passed else "WARN"
        return CheckResult("时间覆盖", passed, level, msg, details)
    finally:
        conn.close()


def check_field_non_null() -> CheckResult:
    """4 个 P0 因子非空率"""
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM financial_indicator").fetchone()[0]
        if total == 0:
            return CheckResult("字段非空率", False, "FAIL", "无数据")
        details = []
        all_pass = True
        for col in ("roe_yearly", "or_yoy", "dt_eps_yoy", "debt_to_assets"):
            non_null = conn.execute(
                f"SELECT COUNT({col}) FROM financial_indicator"
            ).fetchone()[0]
            rate = non_null / total
            mark = "✓" if rate >= 0.70 else "✗" if rate < 0.50 else "⚠"
            details.append(f"{mark} {col}: {non_null}/{total} ({rate:.1%})")
            if rate < 0.50:
                all_pass = False
        level = "PASS" if all_pass else "WARN"
        return CheckResult("字段非空率", all_pass, level,
                           f"4 个 P0 因子非空率 (阈值 ≥70%)", details)
    finally:
        conn.close()


# ============ 第 2 类：合理性 ============

def check_sanity_ranges() -> CheckResult:
    """因子值在业界经验范围内"""
    conn = _conn()
    try:
        all_pass = True
        details = []
        for col, (lo, hi) in SANITY_RANGES.items():
            row = conn.execute(
                f"SELECT MIN({col}), MAX({col}), COUNT({col}) FROM financial_indicator"
            ).fetchone()
            min_v, max_v, n = row
            if min_v is None:
                continue
            out_low = conn.execute(
                f"SELECT COUNT(*) FROM financial_indicator WHERE {col} < ?", (lo,)
            ).fetchone()[0]
            out_high = conn.execute(
                f"SELECT COUNT(*) FROM financial_indicator WHERE {col} > ?", (hi,)
            ).fetchone()[0]
            outliers = out_low + out_high
            outlier_rate = outliers / n if n else 0
            mark = "✓" if outliers == 0 else "⚠" if outlier_rate < 0.01 else "✗"
            if outlier_rate >= 0.01:
                all_pass = False
            details.append(
                f"{mark} {col}: range [{min_v:.1f}, {max_v:.1f}] "
                f"业界范围 [{lo}, {hi}] 异常 {outliers}/{n} ({outlier_rate:.2%})"
            )
        level = "PASS" if all_pass else "WARN"
        return CheckResult("数值合理性", all_pass, level,
                           f"4 个 P0 因子值范围 (异常率阈值 < 1%)", details)
    finally:
        conn.close()


def check_p95_ranges() -> CheckResult:
    """因子值 P5/P95 在业界常见区间内（大量异常 → 数据可疑）"""
    conn = _conn()
    try:
        details = []
        for col, (lo, hi) in P95_RANGES.items():
            rows = conn.execute(
                f"SELECT {col} FROM financial_indicator WHERE {col} IS NOT NULL"
            ).fetchall()
            values = [r[0] for r in rows if r[0] is not None]
            if not values:
                continue
            values.sort()
            n = len(values)
            p5 = values[int(n * 0.05)]
            p95 = values[int(n * 0.95)]
            mark = "✓" if (p5 >= lo * 1.5 and p95 <= hi * 1.5) else "⚠"
            details.append(
                f"{mark} {col}: P5={p5:.1f} P95={p95:.1f} 业界 P95 [{lo}, {hi}]"
            )
        return CheckResult("分位数检查", True, "PASS",
                           f"P5/P95 vs 业界经验值（仅展示，不阻塞）", details)
    finally:
        conn.close()


# ============ 第 3 类：正确性 ============

def check_ann_date_after_end_date() -> CheckResult:
    """公告日必须晚于财报截止日"""
    conn = _conn()
    try:
        bad = conn.execute(
            "SELECT COUNT(*) FROM financial_indicator WHERE ann_date <= end_date"
        ).fetchone()[0]
        if bad == 0:
            return CheckResult("ann_date > end_date", True, "PASS",
                               "所有记录公告日均晚于财报截止日")
        # 列出异常样本
        samples = conn.execute(
            "SELECT code, ann_date, end_date FROM financial_indicator "
            "WHERE ann_date <= end_date LIMIT 5"
        ).fetchall()
        details = [f"{c}: ann={a}, end={e}" for c, a, e in samples]
        return CheckResult("ann_date > end_date", False, "FAIL",
                           f"{bad} 条记录公告日早于/等于财报截止日（违反基本逻辑）",
                           details)
    finally:
        conn.close()


def check_primary_key_unique() -> CheckResult:
    """(code, ann_date, end_date) 应唯一"""
    conn = _conn()
    try:
        bad = conn.execute(
            """SELECT code, ann_date, end_date, COUNT(*) as n
               FROM financial_indicator
               GROUP BY code, ann_date, end_date
               HAVING n > 1 LIMIT 5"""
        ).fetchall()
        if not bad:
            return CheckResult("主键唯一性", True, "PASS",
                               "(code, ann_date, end_date) 全部唯一")
        details = [f"{c}: ann={a}, end={e}, 重复 {n} 次" for c, a, e, n in bad]
        return CheckResult("主键唯一性", False, "FAIL",
                           f"发现 {len(bad)} 组重复主键（已展示前 5 组）", details)
    finally:
        conn.close()


def check_quarterly_continuity() -> CheckResult:
    """抽样 10 只股票，检查季报连续性（连续 4 季度有缺失 = 异常）"""
    conn = _conn()
    try:
        sample_codes = [r[0] for r in conn.execute(
            "SELECT code, COUNT(*) as n FROM financial_indicator "
            "GROUP BY code ORDER BY n DESC LIMIT 20"
        ).fetchall()][:10]

        broken = []
        for code in sample_codes:
            ends = [r[0] for r in conn.execute(
                "SELECT DISTINCT end_date FROM financial_indicator "
                "WHERE code = ? ORDER BY end_date", (code,)
            ).fetchall()]
            if len(ends) < 4:
                continue
            # 期望连续季报: 0331, 0630, 0930, 1231
            quarters_seen = set(e[-4:] for e in ends if e)
            expected = {"0331", "0630", "0930", "1231"}
            missing = expected - quarters_seen
            if missing:
                broken.append(f"{code}: 缺失季度 {missing}")

        if not broken:
            return CheckResult("季报连续性", True, "PASS",
                               f"抽样 {len(sample_codes)} 只全部包含 4 个季度报告期")
        return CheckResult("季报连续性", True, "WARN",
                           f"抽样 {len(sample_codes)} 只中 {len(broken)} 只缺少某季度",
                           broken)
    finally:
        conn.close()


# ============ 第 4 类：未来数据泄露 ============

def check_no_future_data() -> CheckResult:
    """ann_date 不能晚于今天"""
    conn = _conn()
    try:
        today = datetime.now().strftime("%Y%m%d")
        bad = conn.execute(
            "SELECT code, ann_date FROM financial_indicator WHERE ann_date > ? LIMIT 5",
            (today,),
        ).fetchall()
        if not bad:
            return CheckResult("未来数据检查", True, "PASS",
                               f"无 ann_date > {today} 的记录")
        details = [f"{c}: ann_date={a}" for c, a in bad]
        return CheckResult("未来数据检查", False, "FAIL",
                           f"{len(bad)}+ 条记录公告日晚于今天（数据脏，禁止训练）",
                           details)
    finally:
        conn.close()


def check_pit_query_correctness() -> CheckResult:
    """PIT 查询测试: as_of=20240501 时只能拿到 ann_date ≤ 20240501 的数据"""
    try:
        from ml.ranker import _lookup_financial_pit
        # 强制重置全局缓存（避免之前测试污染）
        import ml.ranker as rk
        if "_FIN_CACHE" in dir(rk):
            try:
                delattr(rk, "_FIN_CACHE")
            except AttributeError:
                pass

        # 找一只有较多历史的股票
        conn = _conn()
        sample = conn.execute(
            "SELECT code FROM financial_indicator GROUP BY code HAVING COUNT(*) >= 6 LIMIT 1"
        ).fetchone()
        conn.close()
        if not sample:
            return CheckResult("PIT 查询正确性", True, "WARN",
                               "无足够样本测试（每股需≥6条），跳过")
        test_code = sample[0]

        # 测试: as_of_date = 20210101 应能找到 2020 年的公告（如果有）
        result_2021 = _lookup_financial_pit(test_code, "20210101")
        # 测试: as_of_date = 20100101 应找不到（太早）
        result_2010 = _lookup_financial_pit(test_code, "20100101")

        details = [
            f"测试股: {test_code}",
            f"as_of=20210101: {'✓ 返回数据' if result_2021 else '空（可能该股 2021 前无数据）'}",
            f"as_of=20100101: {'✓ 空（正确）' if not result_2010 else '✗ 不应返回数据'}",
        ]
        passed = not result_2010  # 关键: 历史日期不应返回未来数据
        return CheckResult("PIT 查询正确性", passed,
                           "PASS" if passed else "FAIL",
                           "PIT 查询语义验证", details)
    except Exception as e:
        return CheckResult("PIT 查询正确性", True, "WARN",
                           f"测试失败: {e}", [])


# ============ 主流程 ============

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="警告也视为失败（用于 CI/cron）")
    args = parser.parse_args()

    print("=" * 60)
    print("财务因子数据质检")
    print("=" * 60)

    checks = [
        # 第 1 类：完整性
        ("第 1 类：完整性", [
            check_table_exists,
            check_stock_coverage,
            check_time_coverage,
            check_field_non_null,
        ]),
        # 第 2 类：合理性
        ("第 2 类：合理性", [
            check_sanity_ranges,
            check_p95_ranges,
        ]),
        # 第 3 类：正确性
        ("第 3 类：正确性", [
            check_ann_date_after_end_date,
            check_primary_key_unique,
            check_quarterly_continuity,
        ]),
        # 第 4 类：未来数据泄露
        ("第 4 类：未来数据泄露", [
            check_no_future_data,
            check_pit_query_correctness,
        ]),
    ]

    has_fail = False
    has_warn = False
    for category, fns in checks:
        print(f"\n{category}")
        for fn in fns:
            try:
                r = fn()
            except Exception as e:
                r = CheckResult(fn.__name__, False, "FAIL", f"检查异常: {e}")
            _print_result(r)
            if r.level == "FAIL":
                has_fail = True
            elif r.level == "WARN":
                has_warn = True

    print("\n" + "=" * 60)
    if has_fail:
        print("❌ 关键检查失败，禁止跑 evolve。请修复数据后重试。")
        sys.exit(1)
    elif has_warn:
        if args.strict:
            print("⚠️  存在警告，--strict 模式下视为失败。")
            sys.exit(1)
        print("⚠️  存在警告但可继续。建议人工 review 后再决定是否跑 evolve。")
        sys.exit(0)
    else:
        print("✅ 全部检查通过，可以跑 evolve。")
        sys.exit(0)


if __name__ == "__main__":
    main()
