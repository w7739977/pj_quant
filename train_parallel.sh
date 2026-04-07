#!/bin/bash
# 并行训练脚本

CHUNKS=4

echo "======================================"
echo "并行 XGBoost 模型训练"
echo "======================================"
echo ""
echo "步骤:"
echo "  1. 拆分股票列表 → $CHUNKS 个 chunk"
echo "  2. 并行处理数据准备 (后台进程)"
echo "  3. 合并训练样本"
echo "  4. 训练 XGBoost 模型"
echo ""

# Step 1: 拆分
echo "[1/4] 拆分股票列表..."
python3 -c "from ml.parallel_prepare import split_chunks; split_chunks($CHUNKS)"
echo ""

# Step 2: 并行准备数据
echo "[2/4] 并行数据准备 (后台)..."
PIDS=""
for i in $(seq 0 $((CHUNKS-1))); do
    python3 -c "from ml.parallel_prepare import prepare_chunk; prepare_chunk($i)" > /tmp/chunk_${i}.log 2>&1 &
    PIDS="$PIDS $!"
    echo "  启动 Agent $i (PID: $!)"
done
echo ""

# 等待所有后台进程完成
echo "等待数据准备完成..."
for pid in $PIDS; do
    wait $pid
    echo "  PID $pid 完成"
done
echo ""

# Step 3: 合并
echo "[3/4] 合并训练样本..."
python3 -c "from ml.parallel_prepare import merge_chunks; df = merge_chunks(); print(f'总样本数: {len(df)}')"
echo ""

# Step 4: 训练模型
echo "[4/4] 训练模型..."
python3 main.py train

echo ""
echo "======================================"
echo "训练完成！"
echo "======================================"
