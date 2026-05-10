#!/usr/bin/env bash
# 从生产拉数据快照覆盖本地，让本地 = 生产快照 + 待开发改动
#
# 用法:
#   bash scripts/sync_from_prod.sh                     # 全部拉
#   bash scripts/sync_from_prod.sh --dry-run           # 看会传什么不实拉
#   bash scripts/sync_from_prod.sh --skip-db           # 跳过 quant.db (1.1GB)
#
# 前置: ssh 已配密钥免密 (推荐 ssh-copy-id ${REMOTE_USER}@${REMOTE_HOST})
#       否则每个 rsync 命令会提示一次密码。
#
# 设计：生产是 SOT。本地 cache / portfolio / logs 不再可信，
#       开发前跑一次同步，开发期间所见即生产。

set -euo pipefail

REMOTE_USER="${REMOTE_USER:-ubuntu}"
REMOTE_HOST="${REMOTE_HOST:-119.91.53.223}"
REMOTE_PATH="${REMOTE_PATH:-/home/ubuntu/pj_quant}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DRY=""
SKIP_DB=0
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY="--dry-run" ;;
        --skip-db) SKIP_DB=1 ;;
        *) echo "未知参数: $arg"; exit 2 ;;
    esac
done

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"
# ssh keepalive: 之前实操中 scp 单连接传 1.1GB 在 51 分钟时被远端 idle-kill
# (Connection closed by remote host)。keepalive 防止此类中断；--partial
# --inplace 让中断后下次跑能续传剩余字节而不是重头来
export RSYNC_RSH="ssh -o StrictHostKeyChecking=accept-new -o ServerAliveInterval=30 -o ServerAliveCountMax=20 -o TCPKeepAlive=yes -o LogLevel=ERROR"
RSYNC="rsync -azh --partial --inplace --info=progress2 ${DRY}"
TS="$(date +%Y%m%d_%H%M%S)"

echo "=== sync_from_prod (${REMOTE}:${REMOTE_PATH}) ==="

# 1. quant.db (生产 SOT) — 备份本地后覆盖
if [[ "$SKIP_DB" -eq 0 ]]; then
    if [[ -f data/quant.db ]] && [[ -z "$DRY" ]]; then
        echo "[1/3] 备份本地 data/quant.db -> data/quant.db.bak.local_${TS}"
        mv data/quant.db "data/quant.db.bak.local_${TS}"
    fi
    echo "[1/3] 拉生产 quant.db (~1.1GB, 几分钟)..."
    $RSYNC "${REMOTE}:${REMOTE_PATH}/data/quant.db" data/quant.db
else
    echo "[1/3] 跳过 quant.db (--skip-db)"
fi

# 2. 模型 (ml/models) — 整目录同步
echo "[2/3] 拉生产 ml/models/..."
mkdir -p ml/models
$RSYNC --include='*.json' --exclude='*' "${REMOTE}:${REMOTE_PATH}/ml/models/" ml/models/

# 3. signals 归档 (只读参考)
echo "[3/3] 拉生产 logs/signals/..."
mkdir -p logs/signals
$RSYNC --delete-after "${REMOTE}:${REMOTE_PATH}/logs/signals/" logs/signals/

if [[ -n "$DRY" ]]; then
    echo "=== dry-run 完成 ==="
    exit 0
fi

# 验证：cache 状态 + 模型版本
echo "=== 验证 ==="
python3 - <<'PY'
import sqlite3, json, os
c = sqlite3.connect("data/quant.db")
n, dates, mn, mx = c.execute(
    "SELECT COUNT(*), COUNT(DISTINCT date), MIN(date), MAX(date) FROM daily_scored_cache"
).fetchone()
print(f"  daily_scored_cache: {n} 行 / {dates} 天 / {mn} ~ {mx}")
holdings = c.execute("SELECT cash, holdings FROM portfolio LIMIT 1").fetchone()
if holdings:
    print(f"  portfolio: cash={holdings[0]:.2f}")
if os.path.exists("ml/models/model_history.json"):
    with open("ml/models/model_history.json") as f:
        cur = json.load(f).get("current", {})
    print(f"  model: {cur.get('date')} R²={cur.get('cv_r2_mean')}")
PY

echo "=== sync 完成 ==="
