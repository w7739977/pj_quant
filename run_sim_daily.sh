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

# 判断是否 A 股交易日（排除周末 + 法定节假日 + 调休补班的周六）
IS_TRADING=$(python -c "
import datetime
d = datetime.date.today()
# 周六/周日一律不开盘（即使是调休补班日，证券交易所也不开）
if d.weekday() >= 5:
    print('no')
else:
    try:
        import chinese_calendar
        print('yes' if chinese_calendar.is_workday(d) else 'no')
    except Exception:
        print('yes')
")

if [ "$IS_TRADING" != "yes" ]; then
    echo "今天不是交易日，跳过"
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

echo "交易日，启动模拟盘引擎..."
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
