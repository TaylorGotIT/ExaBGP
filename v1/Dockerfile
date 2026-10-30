FROM alpine:latest

# 安装依赖
RUN apk add --no-cache python3 py3-pip

# 虚拟环境
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 安装 exabgp
RUN pip install --upgrade pip
RUN pip install --no-cache-dir exabgp

# 创建 exabgp 工作目录
RUN mkdir -p /opt/venv/etc/exabgp

# 生成环境文件
RUN exabgp env > /opt/venv/etc/exabgp/exabgp.env

# 管道文件
RUN mkdir -p /run/exabgp \
    && mkfifo /run/exabgp/exabgp.in /run/exabgp/exabgp.out \
    && chmod 777 /run/exabgp/exabgp.*

# 把配置文件 复制到 容器工作目录（关键！）
COPY exabgp.conf /opt/venv/etc/exabgp/exabgp.conf

# 进入配置目录
WORKDIR /opt/venv/etc/exabgp

EXPOSE 179/tcp

# 你要的命令！
CMD ["exabgp", "server", "exabgp.conf"]
