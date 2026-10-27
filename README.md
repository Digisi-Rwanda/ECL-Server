# ECL-Server

Production FastAPI authentication backend for the ECL Platform.

## Quick start

```bash
cd ECL-Server
make setup              # once: create .venv and install dependencies
cp .env.example .env    # once: configure environment
make generate-keys      # once: paste keys into .env
```

### Development (two terminals — separate logs)

**Terminal 1 — infrastructure + Celery:**

```bash
make dev
```

**Terminal 2 — API backend only:**

```bash
make api
```

Verify the API is healthy:

```bash
curl -s http://localhost:8000/health | python3 -m json.tool
```

Stop everything when done:

```bash
make stop
```

Do **not** run bare `uvicorn` from your shell — it uses system Python and will fail with `ModuleNotFoundError: No module named 'fastapi'`. Always use `make api`.

### Combined logs (optional)

If you prefer API + Celery in one terminal (mixed logs):

```bash
make up && make migrate
make dev-all
```

Verify Redis is reachable: `make dev-check`

Email troubleshooting guide: [docs/EMAIL_DIAGNOSIS_PROMPT.md](docs/EMAIL_DIAGNOSIS_PROMPT.md)

Live Resend smoke test: `RESEND_TEST_RECIPIENT=you@example.com pytest tests/test_email_smoke.py -m resend -v`

## Tests

```bash
docker compose -f docker/docker-compose.test.yml up -d
make test
```

## Production Docker (Render)

The backend deploys as a **single Docker container** on Render running both the FastAPI API and Celery worker/beat via supervisord. The frontend (ECL-Web) deploys separately on Vercel and connects to this backend.

Full deployment guide: [DOCKER_IMPLEMENTATION.md](DOCKER_IMPLEMENTATION.md)

**Quick local Docker test:**

```bash
docker build -f docker/Dockerfile -t ecl-server:local .
docker run --rm -p 8000:8000 -e PORT=8000 --env-file .env ecl-server:local
curl http://localhost:8000/ready
```

**Render settings:** Runtime = Docker, Dockerfile Path = `docker/Dockerfile`, Docker Context = `.`, Health Check = `/ready`. Leave Build Command and Start Command blank.

Native Render scripts (`scripts/start_production.sh`, `scripts/start_worker.sh`) are for the legacy Native Python runtime only — not used when deploying via Docker.

## Frontend contract

Read-only reference: `../ECL-Web/`. Full spec: `ECL_AUTH_BACKEND_PROMPT.md`.
