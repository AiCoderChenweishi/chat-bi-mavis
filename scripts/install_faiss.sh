#!/bin/bash
# scripts/install_faiss.sh — 在 server 上装 faiss-cpu + bge 模型 (跟 chat-bi-mavis 同进程, 零 systemd)
# 适用: Ubuntu 22.04+ / Debian 12+ / 内存 ≥ 512M
# 用法: bash scripts/install_faiss.sh
set -e

BGE_DIR="/opt/models/bge-small-zh-v1.5"
WORK_DIR="/opt/data-analyst-agent"

echo "=== 1. 装系统依赖 (apt) ==="
apt-get update -qq 2>&1 | tail -2
apt-get install -y -qq python3-pip python3-venv 2>&1 | tail -2
echo "---"

echo "=== 2. 装 Python 依赖 (faiss-cpu + jieba + onnxruntime + tokenizers) ==="
if [ -d "$WORK_DIR/venv" ]; then
    . "$WORK_DIR/venv/bin/activate"
    pip install -i https://mirrors.aliyun.com/pypi/simple/ \
        faiss-cpu jieba onnxruntime tokenizers 2>&1 | tail -3
else
    echo "  ✗ $WORK_DIR/venv 不存在, 请先跑 deploy_chat_bi_*.sh 装 chat-bi-mavis"
    exit 1
fi
echo "---"

echo "=== 3. 下 bge-small-zh-v1.5 ONNX 模型 (91MB) ==="
if [ -f "$BGE_DIR/model.onnx" ] && [ -f "$BGE_DIR/tokenizer.json" ]; then
    echo "  ✓ 模型已在 $BGE_DIR"
else
    mkdir -p "$BGE_DIR"
    echo "  → 下 model.onnx"
    curl -sSL -o "$BGE_DIR/model.onnx" "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/onnx/model.onnx"
    echo "  → 下 tokenizer.json"
    curl -sSL -o "$BGE_DIR/tokenizer.json" "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/tokenizer.json"
    echo "  ✓ 模型就绪"
fi
echo "---"

echo "=== 4. 配 chat-bi-mavis .env (用 faiss) ==="
# 删 ES_URL (faiss 不需要)
sed -i '/^CHAT_BI_ES_URL=/d' "$WORK_DIR/.env"
# 加 faiss 配置
if ! grep -q "CHAT_BI_EMBED_DIR" "$WORK_DIR/.env"; then
    echo "CHAT_BI_EMBED_DIR=$BGE_DIR" >> "$WORK_DIR/.env"
fi
echo "  .env 配好 (移除 ES_URL, 保留 EMBED_DIR)"
echo "---"

echo "=== 5. 重启 chat-bi-mavis 让 faiss 生效 ==="
systemctl restart chat-bi-mavis 2>/dev/null && echo "  ✓ restart ok" || echo "  ! restart 失败 (systemd 没装?)"
echo "---"

echo "=== 6. 验证 (等 5s) ==="
sleep 5
if curl -sS -m 5 http://127.0.0.1:8000/health 2>&1 | grep -q "ok"; then
    echo "  ✓ chat-bi-mavis up"
    echo "---"
    echo "=== 7. 灌 KB (从 sqlite 重建 faiss) ==="
    cd "$WORK_DIR"
    . venv/bin/activate
    export CHAT_BI_EMBED_DIR="$BGE_DIR"
    python -m scripts.reindex_faiss 2>&1 | tail -10
else
    echo "  ! chat-bi-mavis 没启, 跳过灌 KB"
    echo "  自己灌: cd $WORK_DIR && . venv/bin/activate && python -m scripts.reindex_faiss"
fi
echo ""
echo "=== 卸载 ==="
echo "  pip uninstall -y faiss-cpu jieba onnxruntime tokenizers"
echo "  rm -rf $BGE_DIR"
echo "  rm $WORK_DIR/data/faiss.index $WORK_DIR/data/faiss_ids.json"
