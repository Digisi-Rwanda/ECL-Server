#!/bin/bash
set -e

echo "Running database migrations..."
.venv/bin/alembic upgrade head

echo "Starting API server..."
exec .venv/bin/uvicorn app.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8000}" \
  --no-access-log
