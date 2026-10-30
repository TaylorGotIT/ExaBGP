FROM alpine:3.19

RUN apk add --no-cache python3 py3-pip

# 虚拟环境
RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

# 安装依赖
RUN pip install --upgrade pip
RUN pip install --no-cache-dir exabgp tornado

# 工作目录
WORKDIR /exabgp

# 端口
EXPOSE 179/tcp
EXPOSE 9550/tcp

# 健康检查
# HEALTHCHECK --interval=10s --timeout=2s --retries=3 --start-period=20s \
#    CMD pgrep exabgp > /dev/null 2>&1 && echo "exabgp running" || echo "exabgp stopped"; exit 0

# 启动命令（运行时创建管道 + 启动 ExaBGP）
CMD ["/bin/sh", "-c", "\
    mkdir -p /run/exabgp && \
    mkfifo /run/exabgp/exabgp.in /run/exabgp/exabgp.out 2>/dev/null && \
    chmod 777 /run/exabgp/exabgp.* 2>/dev/null && \
    sleep 1 && \
    pkill -f 'http_api.py' 2>/dev/null || true && \
    sleep 1 && \
    exabgp server --once exabgp.conf \
"]
