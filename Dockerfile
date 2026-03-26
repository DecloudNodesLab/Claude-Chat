FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl wget git vim nano less procps \
    coreutils findutils grep sed gawk tar gzip unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Download xterm.js static files locally so CDN is not needed
RUN mkdir -p /app/static/xterm && \
    curl -fsSL "https://unpkg.com/xterm@5.3.0/lib/xterm.js" \
         -o /app/static/xterm/xterm.js && \
    curl -fsSL "https://unpkg.com/xterm@5.3.0/css/xterm.css" \
         -o /app/static/xterm/xterm.css && \
    curl -fsSL "https://unpkg.com/@xterm/addon-fit@0.8.0/lib/addon-fit.js" \
         -o /app/static/xterm/addon-fit.js

COPY app/ ./app/
COPY templates/ ./templates/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

RUN mkdir -p /workspace /data/chats

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

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${APP_PORT}/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
