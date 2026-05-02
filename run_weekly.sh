#!/bin/bash
# =============================================================
# 周一盘前 D 方案共识选股推送（08:30）
#
# 流程:
#   - preflight 健康检查
#   - 共识选股（读上周 Mon-Fri cache → 频次共识 → top 10）
#   - 推送微信
#
# crontab:
#   30 8 * * 1 /path/to/pj_quant/run_weekly.sh >> /path/to/pj_quant/logs/cron.log 2>&1
#
# 配套要求:
#   run_daily.sh 必须每天 15:15 跑（Mon-Fri），保证 cache 持续累积
# =============================================================

set -e

cd "$(dirname "$0")"

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/weekly_${DATE}.log"

echo "========== $DATE 周一盘前共识选股 ==========" | tee -a "$LOG_FILE"

# 阶段一：preflight
echo "[$(date +%H:%M:%S)] preflight 检查..." | tee -a "$LOG_FILE"
if python3 scripts/preflight.py 2>&1 | tee -a "$LOG_FILE"; then
    echo "[$(date +%H:%M:%S)] preflight 通过" | tee -a "$LOG_FILE"
else
    echo "[$(date +%H:%M:%S)] preflight 失败，推送告警" | tee -a "$LOG_FILE"
    python3 -c "
from alert.notify import send_message
from config.settings import PUSHPLUS_TOKEN
send_message('⚠️  周一盘前 preflight 失败', '$(date) 详见 $LOG_FILE', PUSHPLUS_TOKEN)
" 2>&1 | tee -a "$LOG_FILE" || true
    exit 1
fi

# 阶段二：检查 cache 是否充足
echo "[$(date +%H:%M:%S)] 验证共识缓存..." | tee -a "$LOG_FILE"
CACHE_DAYS=$(python3 -c "
import sys; sys.path.insert(0, '.')
from portfolio.consensus import cache_stats
s = cache_stats()
print(s.get('distinct_dates', 0))
" 2>&1)

if [ "$CACHE_DAYS" -lt 5 ]; then
    echo "[$(date +%H:%M:%S)] ⚠️ cache 仅 $CACHE_DAYS/5 天，自动 backfill..." | tee -a "$LOG_FILE"
    python3 scripts/backfill_consensus_cache.py --days 10 2>&1 | tee -a "$LOG_FILE" || \
        echo "[$(date +%H:%M:%S)] backfill 失败" | tee -a "$LOG_FILE"
fi

# 阶段三：共识选股 + 推送
echo "[$(date +%H:%M:%S)] D 方案共识选股推送..." | tee -a "$LOG_FILE"
if python3 main.py live --consensus --push 2>&1 | tee -a "$LOG_FILE"; then
    echo "[$(date +%H:%M:%S)] 推送完成" | tee -a "$LOG_FILE"
else
    echo "[$(date +%H:%M:%S)] live --consensus 失败" | tee -a "$LOG_FILE"
    python3 -c "
from alert.notify import send_message
from config.settings import PUSHPLUS_TOKEN
send_message('⚠️  周一共识选股失败', '$(date) 详见 $LOG_FILE', PUSHPLUS_TOKEN)
" 2>&1 | tee -a "$LOG_FILE" || true
    exit 1
fi

echo "[$(date +%H:%M:%S)] 周一盘前流程完成" | tee -a "$LOG_FILE"
