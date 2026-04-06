#!/bin/bash
# =============================================================
# A股量化交易系统 - 一键部署脚本
#
# 用法:
#   bash setup.sh              # 完整安装 + 首次运行
#   bash setup.sh --skip-data  # 跳过数据下载（仅安装依赖）
# =============================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "${PROJECT_DIR}"

echo ""
echo "============================================================"
echo "  A股量化交易系统 - 一键部署"
echo "============================================================"
echo ""

# ============ Step 1: 检查 Python ============
echo -e "${YELLOW}[1/6] 检查 Python 环境...${NC}"
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1)
    echo "  ✓ ${PY_VER}"
else
    echo -e "${RED}  ✗ 未找到 python3，请先安装 Python 3.9+${NC}"
    exit 1
fi

# ============ Step 2: 安装依赖 ============
echo -e "${YELLOW}[2/6] 安装 Python 依赖...${NC}"
pip3 install -r requirements.txt --quiet 2>/dev/null || pip install -r requirements.txt --quiet
echo "  ✓ 依赖安装完成"

# ============ Step 3: macOS libomp (XGBoost) ============
echo -e "${YELLOW}[3/6] 检查 XGBoost 依赖...${NC}"
if [[ "$(uname)" == "Darwin" ]]; then
    if ! brew list libomp &>/dev/null 2>&1; then
        echo "  安装 libomp (macOS XGBoost 需要)..."
        brew install libomp 2>/dev/null || echo "  ! 请手动运行: brew install libomp"
    else
        echo "  ✓ libomp 已安装"
    fi
else
    echo "  ✓ 非 macOS，跳过"
fi

# ============ Step 4: 初始化目录 ============
echo -e "${YELLOW}[4/6] 初始化目录结构...${NC}"
mkdir -p logs
mkdir -p ml/models
touch ml/models/.gitkeep
echo "  ✓ 目录就绪"

# ============ Step 5: 下载数据 ============
if [[ "$1" != "--skip-data" ]]; then
    echo -e "${YELLOW}[5/6] 下载历史数据（首次可能需要几分钟）...${NC}"
    python3 main.py fetch
    echo "  ✓ ETF 数据下载完成"
    echo -e "  ${YELLOW}提示: 运行 python3 main.py fetch-all 批量获取全市场股票数据（约30-60分钟）${NC}"
else
    echo -e "${YELLOW}[5/6] 跳过数据下载 (--skip-data)${NC}"
fi

# ============ Step 6: 验证安装 ============
echo -e "${YELLOW}[6/6] 验证安装...${NC}"
python3 -c "
from portfolio.allocator import allocate_capital
from ml.auto_evolve import evolve
from sentiment.analyzer import analyze_market_sentiment
print('  ✓ 所有模块导入正常')

r = allocate_capital(0.0, 20000)
print(f'  ✓ 资金分配测试: {r[\"regime\"]}')
"
echo ""

echo "============================================================"
echo -e "  ${GREEN}部署完成！${NC}"
echo "============================================================"
echo ""
echo "常用命令:"
echo "  python main.py deploy              # 生成今日操作清单"
echo "  python main.py deploy --push       # 生成清单 + 微信推送"
echo "  python main.py backtest            # 运行回测"
echo "  python main.py train               # 训练ML模型"
echo "  python main.py evolve              # 模型自动进化"
echo ""
echo "定时任务（可选）:"
echo "  crontab -e"
echo "  30 15 * * 1-5 ${PROJECT_DIR}/run_daily.sh >> ${PROJECT_DIR}/logs/daily.log 2>&1"
echo "  0 16 1 * * ${PROJECT_DIR}/run_monthly_evolve.sh >> ${PROJECT_DIR}/logs/evolve.log 2>&1"
echo ""
