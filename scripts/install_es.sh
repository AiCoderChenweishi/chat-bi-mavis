#!/bin/bash
# scripts/install_es.sh — 在 server 上装 ES 8.18 + smartcn + bge 模型
# 适用: Ubuntu 22.04 / Debian 12 / 内存 ≥ 1.5G
# 用法: bash scripts/install_es.sh
set -e

ES_VERSION="8.18.0"
ES_DIR="/opt/es/elasticsearch-${ES_VERSION}"
ES_USER="esuser"
ES_HEAP="512m"
BGE_DIR="/opt/models/bge-small-zh-v1.5"

echo "=== 1. 检查内存 (≥ 1.5G) ==="
TOTAL_MEM_M=$(free -m | awk '/^Mem:/ {print $2}')
if [ "$TOTAL_MEM_M" -lt 1500 ]; then
    echo "  ✗ 内存仅 ${TOTAL_MEM_M}M, ES 启动会 OOM. 建议先加内存."
    exit 1
fi
echo "  ✓ 内存 ${TOTAL_MEM_M}M 够用"

echo "=== 2. 检查 ES 是否已装 ==="
if [ -d "$ES_DIR" ]; then
    echo "  ✓ ES 已在 $ES_DIR, 跳下"
else
    echo "  → 下 ES ${ES_VERSION} (618MB)"
    cd /opt
    curl -sSL -o /tmp/es.tar.gz "https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-${ES_VERSION}-linux-x86_64.tar.gz"
    tar -xzf /tmp/es.tar.gz
    rm /tmp/es.tar.gz
    echo "  ✓ 解压到 $ES_DIR"
fi

echo "=== 3. 建 esuser (ES 不能 root 跑) ==="
if id "$ES_USER" &>/dev/null; then
    echo "  ✓ $ES_USER 已存在"
else
    useradd -m -s /bin/bash "$ES_USER"
    echo "  ✓ 创建 $ES_USER"
fi

echo "=== 4. 装 smartcn 插件 (ES 自带中文分词) ==="
chown -R "$ES_USER:$ES_USER" "$ES_DIR"
sudo -u "$ES_USER" "$ES_DIR/bin/elasticsearch-plugin" install analysis-smartcn 2>&1 | tail -3

echo "=== 5. 配 ES config (heap / 单节点 / 禁安全) ==="
cat > "$ES_DIR/config/elasticsearch.yml" << 'EOF'
cluster.name: chat-bi-kb
node.name: chat-bi-kb-1
network.host: 127.0.0.1
http.port: 9200
discovery.type: single-node

xpack.security.enabled: false
xpack.security.enrollment.enabled: false
xpack.security.http.ssl.enabled: false
xpack.security.transport.ssl.enabled: false
xpack.ml.enabled: false
EOF
mkdir -p "$ES_DIR/config/jvm.options.d"
cat > "$ES_DIR/config/jvm.options.d/heap.options" << EOF
-Xms${ES_HEAP}
-Xmx${ES_HEAP}
EOF
chown -R "$ES_USER:$ES_USER" "$ES_DIR/config"
echo "  ✓ heap=${ES_HEAP}, 单节点, 监听 127.0.0.1:9200"

echo "=== 6. 下 bge-small-zh-v1.5 ONNX 模型 ==="
if [ -f "$BGE_DIR/model.onnx" ] && [ -f "$BGE_DIR/tokenizer.json" ]; then
    echo "  ✓ 模型已在 $BGE_DIR"
else
    mkdir -p "$BGE_DIR"
    echo "  → 下 model.onnx (91MB, hf-mirror 镜像)"
    curl -sSL -o "$BGE_DIR/model.onnx" "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/onnx/model.onnx"
    echo "  → 下 tokenizer.json (429KB)"
    curl -sSL -o "$BGE_DIR/tokenizer.json" "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/tokenizer.json"
    echo "  ✓ 模型就绪"
fi
chown -R "$ES_USER:$ES_USER" "$BGE_DIR"

echo "=== 7. 写 systemd service ==="
cat > /etc/systemd/system/chat-bi-es.service << EOF
[Unit]
Description=Elasticsearch for chat-bi-mavis
After=network.target

[Service]
Type=simple
User=$ES_USER
Group=$ES_USER
Environment=ES_JAVA_HOME=$ES_DIR/jdk
Environment=JAVA_HOME=$ES_DIR/jdk
ExecStart=$ES_DIR/bin/elasticsearch
Restart=on-failure
RestartSec=10
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable chat-bi-es.service
echo "  ✓ systemd service 装好 (未启动, 需手动启)"

echo ""
echo "=== 下一步 ==="
echo "  启 ES:"
echo "    sudo systemctl start chat-bi-es"
echo "  查状态 (等 30-60s):"
echo "    systemctl status chat-bi-es"
echo "    curl http://127.0.0.1:9200/_cluster/health"
echo ""
echo "  ES 跑通后, 跑灌库脚本:"
echo "    export CHAT_BI_EMBED_DIR=$BGE_DIR"
echo "    cd /opt/data-analyst-agent && python3 -m scripts.reindex_to_es"
echo ""
echo "  重启 chat-bi-mavis 让它走 ES:"
echo "    sudo systemctl restart chat-bi-mavis"
echo ""
echo "  卸载 ES:"
echo "    sudo systemctl stop chat-bi-es && sudo systemctl disable chat-bi-es"
echo "    sudo rm /etc/systemd/system/chat-bi-es.service"
echo "    sudo rm -rf $ES_DIR $BGE_DIR"
echo "    sudo userdel -r $ES_USER"
