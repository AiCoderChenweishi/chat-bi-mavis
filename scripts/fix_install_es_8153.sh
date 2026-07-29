#!/bin/bash
# /tmp/fix_install_es_8153.sh — 在 8.153 跑, 修 ES 装失败 (用 scp 拷来的 tarball)
set -e

ES_DIR="/opt/es/elasticsearch-8.18.0"
BGE_DIR="/opt/models/bge-small-zh-v1.5"
ES_USER="esuser"
ES_HEAP="512m"

echo "=== 1. 解压 ES + bge bundle ==="
rm -rf /opt/es /opt/models
mkdir -p /opt
cd /opt
tar -xzf /tmp/es-bge-bundle.tar.gz
ls /opt/es/ /opt/models/
echo "---"

echo "=== 2. esuser (ES 不能 root 跑) ==="
if id "$ES_USER" &>/dev/null; then
    echo "  exists"
else
    useradd -m -s /bin/bash "$ES_USER"
fi
chown -R "$ES_USER:$ES_USER" /opt/es /opt/models
echo "  esuser 创建 + chown"
echo "---"

echo "=== 3. 装 smartcn 插件 (走 esuser 跑 elasticsearch-plugin) ==="
sudo -u "$ES_USER" "$ES_DIR/bin/elasticsearch-plugin" install analysis-smartcn 2>&1 | tail -3
echo "---"

echo "=== 4. 配 ES config (heap 512m / 单节点 / 监听 127.0.0.1 / 禁安全) ==="
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
echo "  yml + heap 写好"
echo "---"

echo "=== 5. systemd service ==="
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
systemctl enable chat-bi-es
echo "  systemd 装好"
echo "---"

echo "=== 6. 启 ES (后台, 等 ~60s ready) ==="
systemctl start chat-bi-es
echo "  ES 启动中..."
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15; do
    sleep 5
    if curl -sS -m 2 http://127.0.0.1:9200/_cluster/health 2>&1 | grep -q status; then
        echo "  $((i*5))s: ES up"
        break
    fi
    echo "  $((i*5))s: 等..."
done

echo "---"
echo "=== 7. 状态 ==="
curl -sS http://127.0.0.1:9200/_cluster/health 2>&1 | python3 -m json.tool 2>&1 | head -10
echo "---"
echo "OK ES 部署完成, 现在可走 scripts/reindex_to_es.py 灌数据"
