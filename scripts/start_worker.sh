#!/bin/bash
set -e

echo "Starting Celery worker + beat..."
mkdir -p run
exec .venv/bin/celery \
  -A app.tasks.celery_app worker \
  --beat \
  --loglevel=info \
  --concurrency=1 \
  --pool=solo \
  -n worker@%h \
  --schedule=run/celerybeat-schedule
