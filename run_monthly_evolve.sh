#!/bin/bash
# =============================================================
# A股量化系统 - 每月自动进化脚本
#
# 每月1号执行: 数据更新 → 因子重算 → 模型训练 → 对比 → 替换 → 报告
#
# crontab:
#   0 16 1 * * /path/to/pj_quant/run_monthly_evolve.sh >> /path/to/pj_quant/logs/evolve.log 2>&1
# =============================================================

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
LOG_FILE="${LOG_DIR}/evolve_$(date +%Y%m).log"

mkdir -p "${LOG_DIR}"

echo "========== $(date '+%Y-%m-%d %H:%M:%S') 月度进化开始 ==========" >> "${LOG_FILE}"

cd "${PROJECT_DIR}"

# 自动进化（训练 + 对比 + 替换 + 微信推送报告）
python3 main.py evolve --push >> "${LOG_FILE}" 2>&1

echo "========== $(date '+%Y-%m-%d %H:%M:%S') 月度进化完毕 ==========" >> "${LOG_FILE}"
echo "" >> "${LOG_FILE}"
