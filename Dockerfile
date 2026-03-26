FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl wget git vim nano less procps \
    coreutils findutils grep sed gawk tar gzip unzip \
    ca-certificates xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install tmate
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then TMATE_ARCH="amd64"; \
    elif [ "$ARCH" = "aarch64" ]; then TMATE_ARCH="arm64v8"; \
    else TMATE_ARCH="amd64"; fi && \
    curl -fsSL "https://github.com/tmate-io/tmate/releases/download/2.4.0/tmate-2.4.0-static-linux-${TMATE_ARCH}.tar.xz" \
        -o /tmp/tmate.tar.xz && \
    tar -xf /tmp/tmate.tar.xz -C /tmp && \
    mv /tmp/tmate-*/tmate /usr/local/bin/tmate && \
    chmod +x /usr/local/bin/tmate && \
    rm -rf /tmp/tmate*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY templates/ ./templates/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /workspace /data/chats /root/.tmate

ENV APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    WORKSPACE_DIR=/workspace \
    DATA_DIR=/data \
    DEFAULT_LOCALE=en \
    BASIC_AUTH_USERNAME=admin \
    BASIC_AUTH_PASSWORD=changeme \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${APP_PORT}/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
