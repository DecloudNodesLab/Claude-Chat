FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    git \
    vim \
    nano \
    less \
    procps \
    coreutils \
    findutils \
    grep \
    sed \
    gawk \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user for running the app
# Shell PTY requires the user to exist
RUN useradd -m -u 1000 -s /bin/bash appuser

# Set working directory
WORKDIR /app

# Copy requirements first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app/ ./app/
COPY templates/ ./templates/

# Create directories
RUN mkdir -p /workspace /data && \
    chown -R appuser:appuser /workspace /data /app

# Copy entrypoint
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Switch to non-root user
USER appuser

# Environment defaults
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
