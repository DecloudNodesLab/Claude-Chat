#!/bin/bash
set -e

echo "Starting Claude Workspace..."
echo "  Host: ${APP_HOST}:${APP_PORT}"
echo "  Workspace: ${WORKSPACE_DIR}"
echo "  Data: ${DATA_DIR}"
echo "  Model: ${CLAUDE_MODEL:-claude-opus-4-5}"

mkdir -p "${WORKSPACE_DIR}" "${DATA_DIR}/chats"

exec python -m uvicorn app.main:app \
    --host "${APP_HOST}" \
    --port "${APP_PORT}" \
    --no-access-log
