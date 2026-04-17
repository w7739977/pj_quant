#!/bin/bash
# 模拟盘每日自动运行脚本
# 用法: crontab 中每天 15:30 运行
#   30 15 * * * cd /home/ubuntu/pj_quant && bash run_sim_daily.sh >> logs/sim_daily.log 2>&1

set -e
cd "$(dirname "$0")"

# 激活虚拟环境
source venv/bin/activate

echo "=========================================="
echo "模拟盘每日运行 $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 运行模拟盘单次执行 + 推送日报到微信
python main.py sim --run-once --push

echo ""
echo "完成: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""
