#!/bin/bash

# 1. 下载最新中国IP段
echo "正在下载中国IP列表..."
curl -s https://gaoyifan.github.io/china-operator-ip/china.txt > china.txt

# 💡 安全检查：如果下载失败或文件为空，则不破坏旧配置
if [ ! -s china.txt ]; then
    echo "❌ 错误：中国 IP 列表下载失败或为空，终止生成！"
    exit 1
fi

# 2. 生成完整 ExaBGP 配置
echo "生成 ExaBGP 配置文件..."
cat > exabgp.conf << EOF
process announce-routes {
    run /venv/bin/python /exabgp/http_api.py;
    encoder text;
}

neighbor 192.168.75.241 {
    router-id 192.168.75.244;
    local-address 192.168.75.244;
    local-as 65000;
    peer-as 65001;

    family {
        ipv4 unicast;
    }

    static {
EOF

# 3. 把所有中国路由写入配置
grep -E '^[0-9]' china.txt | awk '{print "        route " $0 " next-hop 192.168.75.1 community 65000:100;"}' >> exabgp.conf

# 4. 关闭配置块
cat >> exabgp.conf << EOF
    }
}
EOF

echo "✅ 完成！已生成完整 exabgp.conf（包含全部中国路由）"
