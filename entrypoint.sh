#!/bin/bash
set -e

echo "Starting Claude Workspace..."
echo "  Host: ${APP_HOST}:${APP_PORT}"
echo "  Workspace: ${WORKSPACE_DIR}"
echo "  Data: ${DATA_DIR}"
echo "  Model: ${CLAUDE_MODEL:-claude-opus-4-5}"

mkdir -p "${WORKSPACE_DIR}" "${DATA_DIR}/chats"

# Copy index.html to workspace on first run so user can edit it via chat
if [ ! -f "${WORKSPACE_DIR}/index.html" ]; then
    cp /app/templates/index.html "${WORKSPACE_DIR}/index.html"
    echo "  Copied index.html to ${WORKSPACE_DIR}/index.html"
fi

exec python -m uvicorn app.main:app \
    --host "${APP_HOST}" \
    --port "${APP_PORT}" \
    --no-access-log
