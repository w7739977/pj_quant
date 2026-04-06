#!/bin/bash
# =============================================================
# A股量化系统 - 一键流水线：数据获取 → 模型训练 → 部署
#
# 用法:
#   bash run_pipeline.sh              # 完整流水线
#   bash run_pipeline.sh --push       # 推送操作清单到微信
#   bash run_pipeline.sh --limit 200  # 只拉200只股票（调试）
# =============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
LOG_FILE="${LOG_DIR}/pipeline_$(date +%Y%m%d_%H%M%S).log"

mkdir -p "${LOG_DIR}"

exec > >(tee -a "${LOG_FILE}") 2>&1

echo ""
echo "============================================================"
echo "  A股量化系统 - 一键流水线"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "============================================================"

cd "${PROJECT_DIR}"

# 解析参数
PUSH=""
LIMIT=""
for arg in "$@"; do
    case $arg in
        --push) PUSH="--push" ;;
        --limit)
            shift
            LIMIT="--limit $1"
            ;;
    esac
done

# ============ Step 1: 批量获取股票行情 ============
echo ""
echo -e "${YELLOW}[1/3] 批量获取股票行情数据...${NC}"
python3 main.py fetch-all ${LIMIT}

# ============ Step 2: 训练 XGBoost 模型 ============
echo ""
echo -e "${YELLOW}[2/3] 训练 XGBoost 模型...${NC}"
python3 main.py train

# ============ Step 3: 生成操作清单 ============
echo ""
echo -e "${YELLOW}[3/3] 生成今日操作清单...${NC}"
python3 main.py deploy ${PUSH} --simulate

echo ""
echo "============================================================"
echo -e "  ${GREEN}流水线执行完毕${NC}"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "  日志: ${LOG_FILE}"
echo "============================================================"
