FROM alpine:3.19

RUN apk add --no-cache python3 py3-pip

RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

RUN pip install --upgrade pip
RUN pip install --no-cache-dir exabgp

RUN mkdir -p /exabgp
WORKDIR /exabgp

EXPOSE 179/tcp

HEALTHCHECK --interval=10s --timeout=2s --retries=3 --start-period=20s \
    CMD pgrep exabgp > /dev/null 2>&1 || exit 1

CMD ["/bin/sh", "-c", "\
    mkdir -p /run/exabgp && \
    mkfifo /run/exabgp/exabgp.in /run/exabgp/exabgp.out && \
    chmod 777 /run/exabgp/exabgp.* && \
    exec exabgp server exabgp.conf; \
"]
