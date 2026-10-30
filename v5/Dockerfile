FROM alpine:latest

# 安装系统基础运行依赖
RUN apk add --no-cache python3 py3-pip

# 创建并隔离 Python 虚拟环境（契合您的路径）
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

# 升级包管理器并安装 ExaBGP 与 Tornado
RUN pip install --upgrade pip && \
    pip install --no-cache-dir exabgp tornado

# 定义容器内部的工作目录
WORKDIR /exabgp

# 将当前目录下的配置、业务引擎、前端网页完全固化进镜像内部
COPY exabgp.conf /exabgp/exabgp.conf
COPY http_api.py /exabgp/http_api.py
COPY index.html /exabgp/index.html

# 暴露服务端口
EXPOSE 179/tcp 9550/tcp

# 启动命令：
# 1. 动态强刷无污染的干净管道
# 2. 运行时利用 sed 动态将环境变量注入配置文件
# 3. 最终拉起主服务进程
CMD ["/bin/sh", "-c", "\
    mkdir -p /run/exabgp && \
    rm -f /run/exabgp/exabgp.* 2>/dev/null && \
    mkfifo /run/exabgp/exabgp.in /run/exabgp/exabgp.out 2>/dev/null && \
    chmod 777 /run/exabgp/exabgp.* 2>/dev/null && \
    \
    sed -i \"s/__TARGET_NEIGHBOR__/${TARGET_NEIGHBOR}/g\" exabgp.conf && \
    sed -i \"s/__ROUTER_ID__/${ROUTER_ID:-$NEXT_HOP}/g\" exabgp.conf && \
    sed -i \"s/__LOCAL_ADDRESS__/${NEXT_HOP}/g\" exabgp.conf && \
    sed -i \"s/__LOCAL_AS__/${LOCAL_AS:-65000}/g\" exabgp.conf && \
    sed -i \"s/__PEER_AS__/${PEER_AS:-65001}/g\" exabgp.conf && \
    \
    sleep 1 \
    && exabgp exabgp.conf \
"]

