#!/bin/bash
# scripts/install_faiss_8153.sh — 8.153 部署 chat-bi-mavis v0.4.1.2 + faiss-cpu
# 治本: 之前 ES OOM kernel 挂, 改 faiss-cpu (同进程, 1G 内存够)
# 跨项目铁律 (2026-07-30 P0 事故): tar --exclude 必须 ./ 前缀
set -e

TARBALL="${1:-/tmp/chat-bi-mavis-v0.5.tar.gz}"
WORK_DIR="/opt/data-analyst-agent"
BGE_DIR="/opt/models/bge-small-zh-v1.5"
DEEPSEEK_KEY="${DEEPSEEK_API_KEY:-sk-5192ec3fc2fa4fdab42be95e1b3a284b}"

if [ ! -f "$TARBALL" ]; then
    echo "✗ tarball 不存在: $TARBALL"
    echo "  先在 sandbox 跑: tar -czf /tmp/chat-bi-mavis-v0.5.tar.gz --exclude='./data' --exclude='./warehouse' --exclude='./.env' --exclude='./reports' --exclude='./static/js' --exclude='./.skills' --exclude='__pycache__' ."
    exit 1
fi

echo "=== 0. tarball 验证 (8 步 deploy 流程) ==="
TAR_SIZE=$(stat -c '%s' "$TARBALL")
TAR_FILES=$(tar -tzf "$TARBALL" | wc -l)
echo "  tarball size: $TAR_SIZE bytes ($(echo "scale=1; $TAR_SIZE/1024" | bc)KB)"
echo "  tarball files: $TAR_FILES"
# 验证 exclude 真生效 (data / warehouse / .env / reports / static/js / .skills 都应该空)
EXCLUDED_HITS=$(tar -tzf "$TARBALL" | grep -cE "^./(data|warehouse|\.env$|reports|static/js|\.skills)" || true)
if [ "$EXCLUDED_HITS" != "0" ]; then
    echo "  ✗ exclude 失效! 命中 $EXCLUDED_HITS 个被排除文件:"
    tar -tzf "$TARBALL" | grep -E "^./(data|warehouse|\.env|reports|static/js|\.skills)" | head -5
    echo "  请用 --exclude='./data' (显式 ./ 前缀) 重新打包"
    exit 1
fi
echo "  ✓ exclude 生效 (data/warehouse/.env/reports/static/js/.skills 全排除)"

echo "---"
echo "=== 1. 解压 chat-bi-mavis ==="
mkdir -p "$WORK_DIR"
cd "$WORK_DIR"
tar -xzf "$TARBALL"
echo "  解压好"
ls -la | head -10
echo "---"

echo "=== 2. 验证 data/ 没被覆盖 (8 步流程 step 5) ==="
if [ -d "data" ] && [ "$(ls -A data 2>/dev/null)" ]; then
    echo "  ✓ data/ 已存在且有内容, 没被覆盖 (解压没破坏老数据)"
    ls -lh data/
else
    echo "  ℹ data/ 是空的或不存在, 正常 (新装或重启后空)"
fi
echo "---"

echo "=== 3. 装系统依赖 (apt) ==="
apt-get update -qq 2>&1 | tail -2
apt-get install -y -qq python3-pip python3-venv nginx 2>&1 | tail -2
echo "---"

echo "=== 4. venv + pip 装 chat-bi-mavis + faiss-cpu + jieba + onnxruntime + tokenizers ==="
python3 -m venv venv
. venv/bin/activate
pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ 2>&1 | tail -1
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
    fastapi 'uvicorn[standard]' pydantic openai httpx duckdb matplotlib \
    faiss-cpu jieba onnxruntime tokenizers 2>&1 | tail -3
echo "---"

echo "=== 5. 下 bge-small-zh-v1.5 ONNX 模型 (91MB) ==="
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

echo "=== 6. 写 .env (DEEPSEEK key + EMBED_DIR + KB_DB) ==="
# 备份老 .env (如有)
[ -f "$WORK_DIR/.env" ] && cp "$WORK_DIR/.env" "$WORK_DIR/.env.bak.$(date +%Y%m%d)" || true
cat > "$WORK_DIR/.env" << EOF
DEEPSEEK_API_KEY=${DEEPSEEK_KEY}
LLM_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
DATA_ANALYST_KB_DB=$WORK_DIR/data/knowledge_base.db
CHAT_BI_EMBED_DIR=$BGE_DIR
HOST=0.0.0.0
PORT=8000
EOF
echo "  .env 写好"
echo "---"

echo "=== 7. 写 systemd service (bash 包装读 .env) ==="
cat > /etc/systemd/system/chat-bi-mavis.service << 'EOF'
[Unit]
Description=chat-bi-mavis server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/data-analyst-agent
ExecStart=/bin/bash -c 'set -a && . ./.env && set +a && exec /opt/data-analyst-agent/venv/bin/python -u server.py'
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
StandardOutput=append:/var/log/chat-bi-mavis.log
StandardError=append:/var/log/chat-bi-mavis.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable chat-bi-mavis
echo "  systemd 写好"
echo "---"

echo "=== 8. nginx 80 反代 8000 ==="
cat > /etc/nginx/sites-available/default << 'NGEOF'
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
        proxy_buffering off;
        proxy_http_version 1.1;
    }
}
NGEOF
nginx -t 2>&1 | head -3
systemctl reload nginx 2>&1 | tail -1
echo "  nginx reload 好"
echo "---"

echo "=== 9. 启 chat-bi-mavis ==="
systemctl start chat-bi-mavis
echo "  started"
for i in 1 2 3 4 5 6 7 8 9 10; do
    sleep 3
    if curl -sS -m 2 http://127.0.0.1:8000/health 2>&1 | grep -q "ok"; then
        echo "  $((i*3))s: chat-bi-mavis up"
        break
    fi
    echo "  $((i*3))s: 等"
done
echo "---"

echo "=== 10. 灌 KB (从 sqlite 全量重建 faiss + FTS5) ==="
. venv/bin/activate
export CHAT_BI_EMBED_DIR="$BGE_DIR"
python3 -m scripts.reindex_faiss 2>&1 | tail -10
echo "---"

echo "=== 11. 端到端验证 (8 步流程 step 7+8) ==="
echo "--- /health ---"
curl -sS http://127.0.0.1:8000/health
echo ""
echo "--- /api/kb/stats ---"
curl -sS http://127.0.0.1:8000/api/kb/stats
echo ""
echo "--- / (走 nginx 80) ---"
curl -sS -m 5 -o /dev/null -w "HTTP %{http_code} | size %{size_download} bytes\n" http://127.0.0.1/
echo "--- 外网验证 (user 实际访问 URL, 跨项目铁律) ---"
EXT_URL="http://$(curl -sS -m 3 ifconfig.me 2>&1 | head -1 | tr -d ' \n')/"
echo "  公网 IP: $(curl -sS -m 3 ifconfig.me 2>&1 | head -1)"
echo "  user 访问 URL (走 80): $EXT_URL"
echo "---"
echo ""
echo "=== 完成 ==="
echo "✓ chat-bi-mavis v0.5 已部署"
echo "  user 访问: http://8.153.192.136/ (走 nginx 80 反代)"
echo "  数据完整性: $(ls $WORK_DIR/data 2>/dev/null | wc -l) 文件在 data/ (sqlite + faiss)"
echo "  后续 user 用时自然累积 KB (page_extract button + workflow auto_extract v0.5)"
