FROM python:3.11-slim

# ExaBGP 4.2.x 在 3.11 上安装稳定；tornado 提供 Web/SSE 控制面
RUN pip install --no-cache-dir "exabgp==4.2.11" tornado

WORKDIR /exabgp

# 引擎 + 前端 + conf 生成器 + 启动入口
COPY confgen.py /exabgp/confgen.py
COPY http_api.py /exabgp/http_api.py
COPY index.html /exabgp/index.html
COPY entrypoint.sh /exabgp/entrypoint.sh
# exabgp 内部选项（text 编码 FIFO API、前台运行、INFO 日志）
COPY exabgp.env /etc/exabgp/exabgp.env

RUN chmod +x /exabgp/entrypoint.sh

EXPOSE 179/tcp 9550/tcp
VOLUME ["/exabgp/data"]

ENTRYPOINT ["/exabgp/entrypoint.sh"]
