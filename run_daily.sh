#!/bin/bash
# =============================================================
# A股量化系统 - 每日 15:15 收盘后维护脚本（Mon-Fri）
#
# 流程（Mon-Fri 全部一致）:
#   - 增量数据拉取
#   - preflight 健康检查
#   - live --monitor-only 推送（持仓止损/止盈监控 + 缓存今日 scored）
#   - postflight 归档
#
# 配套：run_weekly.sh (周一 08:30 盘前) — 跑共识选股推送 picks
#
# 为什么 D 方案不在周一 15:15 跑？
#   A 股 15:00 已收盘。周一 15:15 推 picks 用户最早周二开盘买，
#   timing 与回测「周一开盘买持 5 天」不符，会丢失部分 alpha。
#
# crontab 配置:
#   15 15 * * 1-5 /path/to/pj_quant/run_daily.sh >> /path/to/pj_quant/logs/cron.log 2>&1
# =============================================================

set -e

cd "$(dirname "$0")"

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

DATE=$(date +%Y-%m-%d)
LOG_FILE="$LOG_DIR/daily_${DATE}.log"
WEEKDAY=$(date +%u)  # 1=Mon ... 7=Sun

echo "========== $DATE 日常执行 (周${WEEKDAY}) ==========" | tee -a "$LOG_FILE"

# 阶段零：增量数据拉取（preflight 之前，保证数据新鲜）
echo "[$(date +%H:%M:%S)] 增量数据拉取..." | tee -a "$LOG_FILE"
if python3 main.py fetch-all --incremental 2>&1 | tee -a "$LOG_FILE"; then
    echo "[$(date +%H:%M:%S)] 数据拉取完成" | tee -a "$LOG_FILE"
else
    echo "[$(date +%H:%M:%S)] 增量数据拉取失败，推送告警" | tee -a "$LOG_FILE"
    python3 -c "
from alert.notify import send_message
from config.settings import PUSHPLUS_TOKEN
send_message('⚠️  增量数据拉取失败', '$(date) Tushare 增量更新失败，请排查。详见 $LOG_FILE', PUSHPLUS_TOKEN)
" 2>&1 | tee -a "$LOG_FILE" || true
    exit 1
fi

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

# 阶段二：持仓监控 + 缓存今日 scored（每天一致）
# 共识选股不在此处跑（见 run_weekly.sh，每周一 08:30 盘前推送）
echo "[$(date +%H:%M:%S)] 持仓监控 + 缓存今日 scored..." | tee -a "$LOG_FILE"
if python3 main.py live --monitor-only --push 2>&1 | tee -a "$LOG_FILE"; then
    echo "[$(date +%H:%M:%S)] 推送完成" | tee -a "$LOG_FILE"
else
    echo "[$(date +%H:%M:%S)] live --monitor-only 失败" | tee -a "$LOG_FILE"
    python3 -c "
from alert.notify import send_message
from config.settings import PUSHPLUS_TOKEN
send_message('⚠️  monitor-only 失败', '$(date) 详见 $LOG_FILE', PUSHPLUS_TOKEN)
" 2>&1 | tee -a "$LOG_FILE" || true
    exit 1
fi

# 阶段三：归档今日信号
echo "[$(date +%H:%M:%S)] 归档信号..." | tee -a "$LOG_FILE"
python3 scripts/postflight.py 2>&1 | tee -a "$LOG_FILE" || true

echo "[$(date +%H:%M:%S)] 日常执行完毕" | tee -a "$LOG_FILE"
