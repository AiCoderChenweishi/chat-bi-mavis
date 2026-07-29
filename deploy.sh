#!/bin/bash
# deploy.sh — 在 server 端跑
# 1. 拉 dev 分支最新
# 2. 装依赖
# 3. 建数仓
# 4. systemd 重启
# 5. smoke test

set -e

APP_DIR="/opt/mavis-dev/data-analyst-agent"
BRANCH="dev"

echo "🚀 部署 data-analyst-agent ($BRANCH)"
cd "$APP_DIR"

echo "📥 1/5 拉代码..."
git fetch origin
git reset --hard origin/$BRANCH

echo "📦 2/5 装依赖..."
pip install --quiet --break-system-packages duckdb openai fastapi uvicorn jinja2 pydantic 2>&1 | tail -3 || true

echo "🗃️  3/5 建数仓..."
python3 warehouse/seed_data.py 2>&1 | tail -5
python3 warehouse/metadata_extractor.py 2>&1 | tail -3

echo "🔄 4/5 重启 systemd..."
sudo systemctl restart data-analyst-agent 2>/dev/null || \
    systemctl --user restart data-analyst-agent 2>/dev/null || \
    echo "⚠️  systemd 重启失败,手动起"

sleep 2

echo "🧪 5/5 smoke test..."
curl -s -f http://localhost:8000/health || echo "❌ /health 失败"
echo ""

echo "✅ 部署完成"
echo "   URL:    http://8.153.192.136:8000"
echo "   Health: curl http://localhost:8000/health"
echo "   日志:    sudo journalctl -u data-analyst-agent -f"
