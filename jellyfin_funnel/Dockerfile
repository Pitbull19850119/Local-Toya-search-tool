ARG BUILD_FROM=python:3.12-slim
FROM python:3.12-slim
ARG BUILD_ARCH=amd64
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates jq \
 && rm -rf /var/lib/apt/lists/*
RUN set -eux; \
    case "${BUILD_ARCH}" in aarch64) A=arm64 ;; *) A=amd64 ;; esac; \
    curl -fsSL "https://pkgs.tailscale.com/stable/tailscale_latest_${A}.tgz" -o /tmp/ts.tgz; \
    mkdir /tmp/ts && tar -xzf /tmp/ts.tgz -C /tmp/ts --strip-components=1; \
    mv /tmp/ts/tailscale /tmp/ts/tailscaled /usr/local/bin/; rm -rf /tmp/ts /tmp/ts.tgz
COPY run.sh /run.sh
COPY status.py /status.py
RUN chmod +x /run.sh
CMD ["/run.sh"]
