#!/bin/bash
# 模拟盘每日自动运行脚本
# crontab 每天都跑，脚本内部判断是否交易日
#   35 15 * * * cd /home/ubuntu/pj_quant && bash run_sim_daily.sh >> logs/sim_daily.log 2>&1

set -e
cd "$(dirname "$0")"

source venv/bin/activate

echo "=========================================="
echo "模拟盘每日运行 $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 判断是否交易日（排除周末+法定节假日）
IS_TRADING=$(python -c "
import datetime
try:
    import chinese_calendar
    print('yes' if chinese_calendar.is_workday(datetime.date.today()) else 'no')
except Exception:
    print('yes' if datetime.date.today().weekday() < 5 else 'no')
")

if [ "$IS_TRADING" != "yes" ]; then
    echo "今天不是交易日，跳过"
    echo ""
    exit 0
fi

# 判断是否交易时间（15:00 之后才执行，确保收盘）
HOUR=$(date '+%H')
if [ "$HOUR" -lt 15 ]; then
    echo "未到收盘时间(当前${HOUR}时)，跳过"
    echo ""
    exit 0
fi

# 运行模拟盘
python main.py sim --run-once --push

echo ""
echo "完成: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""
