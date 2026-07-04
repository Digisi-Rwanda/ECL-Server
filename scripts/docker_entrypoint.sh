#!/bin/bash
set -e

echo "==> Running database migrations..."
alembic upgrade head
echo "==> Migrations complete."

echo "==> Starting supervisord (api + worker)..."
exec /usr/local/bin/supervisord -c /app/supervisord.conf
