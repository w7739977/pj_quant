#!/bin/bash
# 模拟盘每日交易引擎
# cron 每天触发，脚本判断交易日，盘中自动运行
#   05 09 * * * cd /home/ubuntu/pj_quant && bash run_sim_daily.sh >> logs/sim_daily.log 2>&1

set -e
cd "$(dirname "$0")"

source venv/bin/activate

echo "=========================================="
echo "模拟盘启动检查 $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 判断是否运行模拟盘（工作日 + 普通周末都跑，仅法定节假日跳过）
SHOULD_RUN=$(python -c "
import datetime
try:
    import chinese_calendar
    d = datetime.date.today()
    if chinese_calendar.is_workday(d):
        print('yes')
    elif d.weekday() >= 5:
        # 周末: 只要不叠加法定节假日就跑（如春节恰逢周末则跳过）
        try:
            _, name = chinese_calendar.get_holiday_detail(d)
            print('yes' if name is None else 'no')
        except Exception:
            print('yes')
    else:
        print('no')
except Exception:
    print('yes')
")

if [ "$SHOULD_RUN" != "yes" ]; then
    echo "今天是法定节假日，跳过"
    echo ""
    exit 0
fi

# 防止重复启动
PIDFILE="/tmp/pj_quant_sim.pid"
if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "模拟盘已在运行 (PID=$OLD_PID)，跳过"
        exit 0
    fi
    rm -f "$PIDFILE"
fi

echo "启动模拟盘引擎..."
echo ""

# 启动引擎（盘中交易 + 收盘推送），记录PID
python -u main.py sim --start --push &
PID=$!
echo $PID > "$PIDFILE"
echo "引擎已启动 PID=$PID"

# 等待进程结束（引擎会在15:00收盘后自动退出）
wait $PID
EXIT_CODE=$?

rm -f "$PIDFILE"

echo ""
echo "引擎已退出 (code=$EXIT_CODE) $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
echo ""
