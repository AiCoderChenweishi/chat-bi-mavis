#!/bin/bash
# /tmp/deploy_chat_bi_8153.sh — 在 8.153 跑 (user-friendly, 不依赖 pexpect heredoc)
set -e
echo "=== 1. 解压 chat-bi-mavis ==="
mkdir -p /opt/data-analyst-agent
cd /opt/data-analyst-agent
tar -xzf /tmp/chat-bi-mavis-v0.4.tar.gz
echo "  解压好, 共 $(ls -1 | wc -l) 项"
echo "---"
echo "=== 2. 装系统依赖 (apt) ==="
apt-get update -qq 2>&1 | tail -2
apt-get install -y -qq python3-pip python3-venv nginx 2>&1 | tail -2
echo "---"
echo "=== 3. venv + pip 装 chat-bi-mavis 依赖 ==="
python3 -m venv venv
. venv/bin/activate
pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ 2>&1 | tail -1
pip install -i https://mirrors.aliyun.com/pypi/simple/ \
    fastapi==0.115.6 'uvicorn[standard]==0.32.1' \
    pydantic==2.10.4 \
    openai httpx duckdb matplotlib \
    'anyio<4' 'sniffio<2' 'starlette<0.40' 'annotated-doc==0.0.3' jinja2 \
    onnxruntime tokenizers 2>&1 | tail -3
echo "---"
echo "=== 4. 写 .env (API key 从 secret 拿, 不用 hardcode) ==="
# API key 从环境变量拿 (mavis 传进来, 不入库)
DEEPSEEK_KEY="${DEEPSEEK_API_KEY:-}"
if [ -z "$DEEPSEEK_KEY" ]; then
    echo "  ⚠️ DEEPSEEK_API_KEY 未传, 跳过写 key (deploy 后手动填 .env)"
fi
cat > /opt/data-analyst-agent/.env << EOF
DEEPSEEK_API_KEY=${DEEPSEEK_KEY}
LLM_PROVIDER=deepseek
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1
DEEPSEEK_MODEL=deepseek-chat
DATA_ANALYST_KB_DB=/opt/data-analyst-agent/data/knowledge_base.db
CHAT_BI_EMBED_DIR=/opt/models/bge-small-zh-v1.5
HOST=0.0.0.0
PORT=8000
EOF
echo "  .env 写好"
echo "---"
echo "=== 5. systemd service ==="
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
StandardOutput=append:/var/log/chat-bi-mavis.log
StandardError=append:/var/log/chat-bi-mavis.log

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
echo "  systemd 写好"
echo "---"
echo "=== 6. nginx 80 反代 8000 ==="
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
systemctl reload nginx
echo "  nginx reload 好"
echo "---"
echo "=== 7. 文件结构 ==="
find . -maxdepth 2 -type f 2>&1 | head -25
echo "---"
echo "OK chat-bi-mavis 部署完成, 待启 ES + chat-bi-mavis"
