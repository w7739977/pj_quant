#!/bin/bash
# =============================================================
# A股量化系统 - 每日统一部署脚本
#
# crontab 配置:
#   每日部署（周一到周五 15:30）:
#     30 15 * * 1-5 /path/to/pj_quant/run_daily.sh >> /path/to/pj_quant/logs/daily.log 2>&1
#
#   每月模型进化（每月1号 16:00）:
#     0 16 1 * * /path/to/pj_quant/run_monthly_evolve.sh >> /path/to/pj_quant/logs/evolve.log 2>&1
# =============================================================

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
LOG_FILE="${LOG_DIR}/daily_$(date +%Y%m%d).log"

mkdir -p "${LOG_DIR}"

echo "========== $(date '+%Y-%m-%d %H:%M:%S') 每日统一部署开始 ==========" >> "${LOG_FILE}"

cd "${PROJECT_DIR}"

# 统一部署: 市场情绪 + 资金分配 + ETF轮动 + 个股精选 + 微信推送 + 模拟执行
python3 main.py deploy --push --simulate >> "${LOG_FILE}" 2>&1

echo "========== $(date '+%Y-%m-%d %H:%M:%S') 每日统一部署完毕 ==========" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
