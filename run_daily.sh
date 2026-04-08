#!/bin/bash
# =============================================================
# A股量化系统 - 每日自动执行脚本
#
# 流程: preflight → live --push → postflight
#
# crontab 配置:
#   30 15 * * 1-5 /path/to/pj_quant/run_daily.sh >> /path/to/pj_quant/logs/cron.log 2>&1
# =============================================================

set -e

cd "$(dirname "$0")"

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/daily_${DATE}.log"

echo "========== $DATE 日常执行 ==========" | tee -a "$LOG_FILE"

# 阶段一：preflight 健康检查
echo "[$(date +%H:%M:%S)] 开始 preflight 检查..." | tee -a "$LOG_FILE"
if python3 scripts/preflight.py 2>&1 | tee -a "$LOG_FILE"; then
    echo "[$(date +%H:%M:%S)] preflight 通过" | tee -a "$LOG_FILE"
else
    echo "[$(date +%H:%M:%S)] preflight 失败，推送告警" | tee -a "$LOG_FILE"
    python3 -c "
from alert.notify import send_message
from config.settings import PUSHPLUS_TOKEN
send_message('⚠️  preflight 失败', '$(date) 日常执行前检查未通过，请排查。详见 $LOG_FILE', PUSHPLUS_TOKEN)
" 2>&1 | tee -a "$LOG_FILE" || true
    exit 1
fi

# 阶段二：生成建议并推送（不动持仓）
echo "[$(date +%H:%M:%S)] 生成操作建议..." | tee -a "$LOG_FILE"
if python3 main.py live --push 2>&1 | tee -a "$LOG_FILE"; then
    echo "[$(date +%H:%M:%S)] 建议推送完成" | tee -a "$LOG_FILE"
else
    echo "[$(date +%H:%M:%S)] live 执行失败" | tee -a "$LOG_FILE"
    python3 -c "
from alert.notify import send_message
from config.settings import PUSHPLUS_TOKEN
send_message('⚠️  live 执行失败', '$(date) 操作建议生成失败，请排查。详见 $LOG_FILE', PUSHPLUS_TOKEN)
" 2>&1 | tee -a "$LOG_FILE" || true
    exit 1
fi

# 阶段三：归档今日信号
echo "[$(date +%H:%M:%S)] 归档信号..." | tee -a "$LOG_FILE"
python3 scripts/postflight.py 2>&1 | tee -a "$LOG_FILE" || true

echo "[$(date +%H:%M:%S)] 日常执行完毕" | tee -a "$LOG_FILE"
