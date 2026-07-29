#!/bin/bash
# scripts/install_faiss_117.sh — 在 117 (prod) 装 faiss-cpu + jieba + bge
# 117 用系统 Python, 不用 venv
set -e

WORK_DIR="/opt/data-analyst-agent"
BGE_DIR="/opt/models/bge-small-zh-v1.5"
DEEPSEEK_KEY="${DEEPSEEK_API_KEY:-}"

echo "=== 1. 拉 v0.4.1 代码 (117 用 main 分支) ==="
cd $WORK_DIR
git fetch origin 2>&1 | tail -2
# 117 老分支是 master, chat-bi-mavis default = main, 切过去
git checkout main 2>&1 | tail -2 || git checkout -b main origin/main
git reset --hard origin/main 2>&1 | tail -3
git log --oneline -3
echo "---"

echo "=== 2. 装 faiss-cpu + jieba + onnxruntime + tokenizers (系统 pip) ==="
pip3 install --break-system-packages -i https://mirrors.aliyun.com/pypi/simple/ \
    faiss-cpu jieba onnxruntime tokenizers 2>&1 | tail -3
echo "---"

echo "=== 3. 下 bge 模型 (91MB) ==="
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

echo "=== 4. 配 .env (清 ES_URL, 加 EMBED_DIR) ==="
# 备份
cp $WORK_DIR/.env $WORK_DIR/.env.bak.$(date +%Y%m%d) 2>/dev/null || true
# 删 ES_URL, 保留 DEEPSEEK_API_KEY
sed -i '/^CHAT_BI_ES_URL=/d' $WORK_DIR/.env
# 加 EMBED_DIR (如有就不加)
if ! grep -q "CHAT_BI_EMBED_DIR" $WORK_DIR/.env; then
    echo "CHAT_BI_EMBED_DIR=$BGE_DIR" >> $WORK_DIR/.env
fi
# 检查 DEEPSEEK key 在不在
if ! grep -q "DEEPSEEK_API_KEY" $WORK_DIR/.env; then
    echo "DEEPSEEK_API_KEY=${DEEPSEEK_KEY}" >> $WORK_DIR/.env
fi
grep -E "DEEPSEEK|EMBED|ES_URL" $WORK_DIR/.env
echo "---"

echo "=== 5. 修 systemd (把 ExecStart 改成读 .env 的 bash 包装) ==="
# 117 systemd ExecStart 是 'python3 -u server.py' 直接, 不读 .env
# venv 跑用 bash -c 'set -a && . ./.env && set +a && exec python3 -u server.py'
cat > /etc/systemd/system/chat-bi-mavis.service << 'EOF'
[Unit]
Description=chat-bi-mavis server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/data-analyst-agent
ExecStart=/bin/bash -c 'set -a && . ./.env && set +a && exec /usr/bin/python3 -u server.py'
Restart=on-failure
RestartSec=5
StandardOutput=append:/var/log/chat-bi-mavis.log
StandardError=append:/var/log/chat-bi-mavis.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo "  systemd 修好 (bash 包装, 读 .env)"
echo "---"

echo "=== 6. 重启 chat-bi-mavis ==="
systemctl restart chat-bi-mavis
echo "  restarted"
sleep 3
systemctl status chat-bi-mavis --no-pager -l 2>&1 | head -3
echo "---"

echo "=== 7. 验证 (等 5s) ==="
sleep 5
if curl -sS -m 5 http://127.0.0.1:8000/health 2>&1 | grep -q "ok"; then
    echo "  ✓ chat-bi-mavis up"
    curl -sS http://127.0.0.1:8000/health 2>&1
    echo
    echo "---"
    echo "=== 8. 灌 KB (从 sqlite 重建 faiss) ==="
    cd $WORK_DIR
    export CHAT_BI_EMBED_DIR="$BGE_DIR"
    python3 -m scripts.reindex_faiss 2>&1 | tail -10
    echo "---"
    echo "=== 9. 验证 faiss ==="
    curl -sS http://127.0.0.1:8000/api/kb/stats 2>&1 | head -1
    echo
else
    echo "  ! chat-bi-mavis 没启, 看日志"
    journalctl -u chat-bi-mavis -n 20 --no-pager 2>&1 | tail -15
fi
