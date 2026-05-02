"""交易日判断工具 — bash 脚本通过 exit code 调用

退出码:
  0 = 命中条件（应继续）
  1 = 不命中（应跳过）

用法:
  python3 scripts/is_trading_day_check.py is_trading      # 今天是否交易日
  python3 scripts/is_trading_day_check.py is_first_of_week # 今天是否本周第一个交易日

例子（在 bash 中）:
  if python3 scripts/is_trading_day_check.py is_trading; then
      echo "今天是交易日"
  else
      echo "非交易日，跳过"
      exit 0
  fi
"""
import sys
from datetime import date, timedelta


def is_trading_day(d: date = None) -> bool:
    """是否中国 A 股交易日（排除周末 + 法定节假日 + 调休补班按工作日处理）"""
    d = d or date.today()
    try:
        import chinese_calendar
        return chinese_calendar.is_workday(d)
    except ImportError:
        # fallback: 仅排除周末
        return d.weekday() < 5
    except NotImplementedError:
        # chinese_calendar 不覆盖此年份
        return d.weekday() < 5


def is_first_trading_day_of_week(d: date = None) -> bool:
    """今天是否是本周（周一开始）的第一个交易日

    场景:
      - 周一是交易日 → 周一返回 True，周二~五返回 False
      - 周一假日 → 周一 False, 周二（若交易日）True
      - 周一二都假 → 周三 True
    """
    d = d or date.today()
    if not is_trading_day(d):
        return False
    # 本周一
    monday = d - timedelta(days=d.weekday())
    # 周一到 d-1 是否已有交易日？
    cur = monday
    while cur < d:
        if is_trading_day(cur):
            return False
        cur += timedelta(days=1)
    return True


def main():
    if len(sys.argv) < 2:
        print("用法: python3 is_trading_day_check.py [is_trading|is_first_of_week]",
              file=sys.stderr)
        sys.exit(2)
    cmd = sys.argv[1]
    today = date.today()
    if cmd == "is_trading":
        ok = is_trading_day(today)
    elif cmd == "is_first_of_week":
        ok = is_first_trading_day_of_week(today)
    else:
        print(f"未知命令: {cmd}", file=sys.stderr)
        sys.exit(2)
    # 同时输出可读结果（stderr 不影响 exit code 判断）
    print(f"{today.isoformat()} {cmd}={'YES' if ok else 'NO'}", file=sys.stderr)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
