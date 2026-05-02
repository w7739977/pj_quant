#!/bin/bash
# =============================================================
# 一键部署 — 拉代码 → 数据 → 校验 → 训练 → 上线 → 缓存 → 冒烟 → 回测
#
# 用法:
#   bash scripts/deploy.sh                    # 全量（含数据拉取，~1-1.5 小时）
#   bash scripts/deploy.sh --quick            # 快速（跳过数据 + 财务 + 回测，~10 分钟）
#   bash scripts/deploy.sh --skip-data        # 跳过日线/估值数据拉取
#   bash scripts/deploy.sh --skip-financial   # 跳过财务因子拉取（季报，月度跑即可）
#   bash scripts/deploy.sh --skip-backtest    # 跳过回测（节省 12-15 分钟）
#   bash scripts/deploy.sh --no-pull          # 不拉代码（已 git pull 过）
#
# 单步选项:
#   --only-code       仅拉代码
#   --only-data       仅拉数据 + 财务 + 校验
#   --only-model      仅训练 + 上线 + 回填 cache
#   --only-verify     仅冒烟 + 回测
# =============================================================

set -e

# ===== 解析参数 =====
SKIP_PULL=false
SKIP_DATA=false
SKIP_FINANCIAL=false
SKIP_BACKTEST=false
QUICK=false
ONLY_MODE=""

for arg in "$@"; do
    case $arg in
        --no-pull)        SKIP_PULL=true ;;
        --skip-data)      SKIP_DATA=true ;;
        --skip-financial) SKIP_FINANCIAL=true ;;
        --skip-backtest)  SKIP_BACKTEST=true ;;
        --quick)          QUICK=true; SKIP_DATA=true; SKIP_FINANCIAL=true; SKIP_BACKTEST=true ;;
        --only-code)      ONLY_MODE="code" ;;
        --only-data)      ONLY_MODE="data" ;;
        --only-model)     ONLY_MODE="model" ;;
        --only-verify)    ONLY_MODE="verify" ;;
        -h|--help)        head -22 "$0" | tail -20; exit 0 ;;
    esac
done

# ===== 切到项目根 =====
cd "$(dirname "$0")/.."
PROJECT_DIR=$(pwd)

# ===== 输出 helper =====
banner() {
    echo ""
    echo "════════════════════════════════════════════════════════════"
    echo "  $1"
    echo "════════════════════════════════════════════════════════════"
}

step_done() {
    echo "  ✅ 完成: $1"
}

step_skip() {
    echo "  ⏭  跳过: $1"
}

step_fail() {
    echo "  ❌ 失败: $1"
    exit 1
}

# ===== Step 计数器 =====
START_TIME=$(date +%s)
banner "🚀 一键部署 ($(date +%Y-%m-%d\ %H:%M))"
echo "  项目: $PROJECT_DIR"
echo "  Python: $(python3 --version)"
[ "$QUICK" = true ]    && echo "  模式: 快速（跳过数据/财务/回测）"
[ -n "$ONLY_MODE" ]    && echo "  模式: only-$ONLY_MODE"

run_code_step() {
    banner "📥 Step 1/8: 拉新代码"
    if [ "$SKIP_PULL" = true ]; then
        step_skip "git pull (--no-pull)"
        return
    fi
    git pull --ff-only origin main || step_fail "git pull"
    step_done "git pull"
}

run_data_step() {
    banner "📊 Step 2/8: 增量数据拉取（行情 + daily_basic）"
    if [ "$SKIP_DATA" = true ]; then
        step_skip "数据拉取 (--skip-data / --quick)"
        return
    fi
    python3 main.py fetch-all --incremental || step_fail "fetch-all"
    step_done "增量数据"
}

run_financial_step() {
    banner "💰 Step 3/8: 财务因子（PIT，季度数据）"
    if [ "$SKIP_FINANCIAL" = true ]; then
        step_skip "财务拉取 (--skip-financial / --quick)"
        return
    fi
    python3 main.py fetch-financial || step_fail "fetch-financial"
    step_done "财务因子"
}

run_validate_step() {
    banner "🔍 Step 4/8: 数据质检"
    if python3 scripts/validate_financial.py; then
        step_done "数据质检通过"
    else
        echo "  ⚠️ 数据质检有警告（不阻塞，继续）"
    fi
}

run_model_step() {
    banner "🧠 Step 5/8: 训练 / 上线 ML 模型"
    python3 main.py evolve || step_fail "evolve"
    step_done "模型已上线 (ml/models/xgb_ranker.json)"
}

run_cache_step() {
    banner "💾 Step 6/8: 回填共识缓存（10 个交易日）"
    python3 scripts/backfill_consensus_cache.py --days 10 || step_fail "backfill cache"
    step_done "共识缓存就绪"
}

run_smoke_step() {
    banner "🧪 Step 7/8: 端到端冒烟（monitor-only，不推送）"
    python3 main.py live --monitor-only || step_fail "live --monitor-only"
    step_done "冒烟通过"
}

run_backtest_step() {
    banner "📈 Step 8/8: 今年以来回测（D 方案 vs 日频）"
    if [ "$SKIP_BACKTEST" = true ]; then
        step_skip "回测 (--skip-backtest / --quick)"
        return
    fi
    python3 scripts/backtest_year.py || step_fail "backtest"
    step_done "回测完成 (logs/backtest_year.csv)"
}

# ===== 主流程 =====
case "$ONLY_MODE" in
    code)
        run_code_step
        ;;
    data)
        run_code_step
        run_data_step
        run_financial_step
        run_validate_step
        ;;
    model)
        run_code_step
        run_model_step
        run_cache_step
        ;;
    verify)
        run_code_step
        run_smoke_step
        run_backtest_step
        ;;
    *)
        # 全流程
        run_code_step
        run_data_step
        run_financial_step
        run_validate_step
        run_model_step
        run_cache_step
        run_smoke_step
        run_backtest_step
        ;;
esac

# ===== 总结 =====
END_TIME=$(date +%s)
ELAPSED=$(( END_TIME - START_TIME ))
MIN=$(( ELAPSED / 60 ))
SEC=$(( ELAPSED % 60 ))

banner "✨ 部署完成 (用时 ${MIN}m${SEC}s)"
echo ""
echo "下一步:"
echo "  1. 检查 crontab: crontab -l"
echo "  2. 等待下一个工作日 15:15 自动运行"
echo "     - 周一 → live --consensus --push"
echo "     - 周二~五 → live --monitor-only --push"
echo ""
echo "手动测试:"
echo "  python3 main.py live --consensus --simulate    # 周一行为（模拟，不动持仓）"
echo "  python3 scripts/backtest_year.py               # 重跑回测"
echo ""
