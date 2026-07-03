#!/bin/bash
set -e

echo "Starting Celery worker + beat..."
mkdir -p run

# Warm shutdown: when Render sends SIGTERM, forward it to Celery so it finishes
# the currently-running task before exiting rather than being killed mid-compute.
_shutdown() {
    echo "SIGTERM received — waiting for current task to finish..."
    kill -TERM "$WORKER_PID" 2>/dev/null
    wait "$WORKER_PID"
}
trap _shutdown SIGTERM SIGINT

.venv/bin/celery \
  -A app.tasks.celery_app worker \
  --beat \
  --loglevel=info \
  --concurrency=1 \
  --pool=solo \
  -n worker@%h \
  --schedule=run/celerybeat-schedule &
WORKER_PID=$!

wait "$WORKER_PID"
