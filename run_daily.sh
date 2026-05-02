#!/bin/bash
# =============================================================
# A股量化系统 - 每日自动执行脚本（D 方案：周一共识选股 + 周二~五持仓监控）
#
# 流程:
#   - 增量数据拉取 (每天)
#   - preflight 健康检查 (每天)
#   - 选股推送 (周一: live --consensus, 周二~五: live --monitor-only)
#   - postflight 归档 (每天)
#
# 决策逻辑（基于 4 个月回测，13 周观测）:
#   - 周一共识选股 D 方案 +1.15% 周均 alpha (vs 日频 +0.41%)
#   - 周二~五 monitor-only 仍跑止损/止盈/超时调仓，但不重选新股
#   - 这样换手降到 1/5，省 0.5%-1%/周 摩擦成本
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

# 阶段二：选股 / 持仓监控（按周几切换模式）
# - 周一: --consensus 触发 5 天频次共识选股
# - 周二~五: --monitor-only 跑止损/止盈 + 缓存今日 scored 供下周共识
if [ "$WEEKDAY" = "1" ]; then
    LIVE_ARGS="live --consensus --push"
    echo "[$(date +%H:%M:%S)] 周一: 共识选股 (D 方案，5 天频次共识)..." | tee -a "$LOG_FILE"
else
    LIVE_ARGS="live --monitor-only --push"
    echo "[$(date +%H:%M:%S)] 周${WEEKDAY}: 仅持仓监控（同时缓存今日 scored 供下周一共识）..." | tee -a "$LOG_FILE"
fi

if python3 main.py $LIVE_ARGS 2>&1 | tee -a "$LOG_FILE"; then
    echo "[$(date +%H:%M:%S)] 推送完成" | tee -a "$LOG_FILE"
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
