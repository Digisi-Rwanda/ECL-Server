# ECL-Server — Docker + supervisord Production Configuration
## Cursor Agent Implementation Prompt

---

## What You Are Building and Why

You are configuring the ECL-Server (a FastAPI + Celery financial computation backend) to run as a **single Docker container on Render** using **supervisord as a process manager**. The container runs two processes simultaneously:

1. **uvicorn** — the FastAPI API server, handling HTTP requests from the Next.js frontend (deployed on Vercel)
2. **celery worker + beat** — the background task queue that processes compute jobs (PD, LGD, EAD credit loss calculations)

The ECL compute pipeline requires both the API (to receive requests and stream results) and the Celery worker (to execute multi-minute compute jobs) to be running at all times. Running both inside one Docker container managed by supervisord guarantees the worker is always co-located with the API and restarts automatically on failure.

This document is the source of truth for production Docker deployment. Follow it exactly — do not improvise or use generic Docker advice that conflicts with this repo.

---

## Project Layout

```
ECL-Server/
├── app/
│   ├── main.py                   ← FastAPI app entry point (/health, /ready)
│   ├── config.py                 ← Settings (reads env vars — see below)
│   ├── tasks/
│   │   ├── celery_app.py         ← Celery configuration
│   │   ├── compute_tasks.py      ← PD/LGD/EAD compute tasks
│   │   ├── email_tasks.py
│   │   └── cleanup_tasks.py
│   ├── modules/runs/router.py    ← Execute endpoint (dispatches pd_task, lgd_task)
│   └── core/redis_ssl.py         ← SSL params for Upstash Redis
├── migrations/                   ← Alembic migration files
├── alembic.ini
├── pyproject.toml
├── docker/
│   ├── Dockerfile                ← PRODUCTION Dockerfile
│   ├── supervisord.conf          ← Process manager config
│   ├── docker-compose.yml        ← LOCAL DEV ONLY (postgres, redis, minio)
│   └── docker-compose.test.yml   ← TEST ISOLATION ONLY
├── scripts/
│   ├── docker_entrypoint.sh      ← Docker: migrations then supervisord
│   ├── start_production.sh       ← Native Render API (uses .venv/bin/)
│   └── start_worker.sh           ← Native Render worker (uses .venv/bin/)
├── Procfile                      ← Native Render web config (not used for Docker)
└── Procfile.prod                 ← Reference for worker flags
```

**Build context:** Render builds with **repo root as context**. Dockerfile path: `docker/Dockerfile`. Docker context: `.`

---

## Container Architecture

```
Render Web Service (Docker, one container)
├── docker_entrypoint.sh
│   ├── alembic upgrade head  (runs once)
│   └── supervisord
│       ├── [program:api]  → uvicorn app.main:app --port $PORT
│       └── [program:worker] → celery worker --beat --pool=solo --concurrency=1

External Services:
├── Neon PostgreSQL       — DATABASE_URL
├── Upstash Redis         — REDIS_CELERY_URL (Celery broker/backend)
├── Redis                 — REDIS_URL (cache + JWT tokens)
└── Cloudflare R2         — STORAGE_* vars
```

---

## Files Implemented

| File | Purpose |
|---|---|
| `docker/Dockerfile` | Multi-stage build, installs supervisor, copies scripts + supervisord.conf |
| `scripts/docker_entrypoint.sh` | Runs migrations once, then exec supervisord |
| `docker/supervisord.conf` | Manages api + worker processes |
| `.dockerignore` | Excludes .venv, tests, git from build context |

---

## Environment Variables (Repo-Accurate)

**Critical:** Use the exact names from `app/config.py` and `.env.example`. Wrong names will cause startup failure in production.

### Required in production

| Variable | Purpose |
|---|---|
| `APP_ENV` | Must be `production` (not `ENVIRONMENT`) |
| `SECRET_KEY` | App secret, 32+ chars |
| `DATABASE_URL` | Neon PostgreSQL, `postgresql+asyncpg://...` |
| `REDIS_URL` | Cache + JWT token storage |
| `REDIS_CELERY_URL` | Upstash Redis for Celery broker/backend |
| `JWT_PRIVATE_KEY` | RS256 JWT signing (base64 PEM) |
| `JWT_PUBLIC_KEY` | RS256 JWT verification (base64 PEM) |
| `STORAGE_ENDPOINT_URL` | R2/S3 endpoint (not `S3_ENDPOINT_URL`) |
| `STORAGE_ACCESS_KEY` | Object storage access key |
| `STORAGE_SECRET_KEY` | Object storage secret key |
| `STORAGE_BUCKET_NAME` | Bucket name |
| `CORS_ORIGINS` | Vercel frontend URL(s), comma-separated |
| `FRONTEND_URL` | Vercel frontend base URL |
| `CSRF_SECRET` | 32+ chars, not dev default |
| `IP_HASH_SALT` | Random string, not dev default |

### Recommended for production Docker

| Variable | Recommended value | Why |
|---|---|---|
| `DATABASE_POOL_SIZE` | `3` | Lower memory/connection use on 512MB shared container |
| `DATABASE_MAX_OVERFLOW` | `5` | Same |
| `LOG_FORMAT` | `json` | Structured logs in Render log viewer |
| `TRUST_PROXY_HEADERS` | `true` | Behind Render load balancer |
| `SENTRY_DSN` | Your Sentry DSN | Error tracking |

### SMTP (email)

| Variable | Notes |
|---|---|
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL` | Not `MAIL_*` names |
| `SUPPRESS_EMAIL_SEND` | Set `true` if Render blocks outbound port 587 |

### JWT / tokens

| Variable | Default |
|---|---|
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | 15 |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | 7 |

---

## Render Dashboard Configuration

Change the existing API Web Service from Native Python to Docker:

| Setting | Value |
|---|---|
| Runtime | Docker |
| Dockerfile Path | `docker/Dockerfile` |
| Docker Context | `.` |
| Build Command | (blank — Docker handles build) |
| Start Command | (blank — CMD in Dockerfile handles it) |
| Health Check Path | `/ready` |

**Why `/ready` not `/health`:** `/ready` only checks database connectivity — fast and reliable for Render liveness. `/health` also inspects Celery workers, storage, and outbox; use it for manual diagnostics, not as the primary health check.

**Port:** Render injects `PORT` automatically. supervisord reads `%(ENV_PORT)s` and passes it to uvicorn.

**If a separate Background Worker service exists:** Suspend or delete it. The worker runs inside this container. Two workers would duplicate task processing.

---

## Local Verification

From `ECL-Server/`:

```bash
# Build
docker build -f docker/Dockerfile -t ecl-server:local .

# Run (replace env values with your real credentials)
docker run --rm \
  -p 8000:8000 \
  -e PORT=8000 \
  -e APP_ENV=production \
  -e SECRET_KEY="your-32-char-secret-key-here-minimum" \
  -e DATABASE_URL="postgresql+asyncpg://..." \
  -e REDIS_URL="rediss://..." \
  -e REDIS_CELERY_URL="rediss://..." \
  -e JWT_PRIVATE_KEY="..." \
  -e JWT_PUBLIC_KEY="..." \
  -e STORAGE_ENDPOINT_URL="https://..." \
  -e STORAGE_ACCESS_KEY="..." \
  -e STORAGE_SECRET_KEY="..." \
  -e STORAGE_BUCKET_NAME="ecl-platform" \
  -e CORS_ORIGINS="https://your-app.vercel.app" \
  -e FRONTEND_URL="https://your-app.vercel.app" \
  -e CSRF_SECRET="your-csrf-secret-32-chars-minimum" \
  -e IP_HASH_SALT="random-salt-string" \
  ecl-server:local
```

**Expected startup logs (in order):**
```
==> Running database migrations...
==> Migrations complete.
==> Starting supervisord (api + worker)...
INFO     supervisord started with pid ...
INFO     spawned: 'api' with pid ...
INFO     spawned: 'worker' with pid ...
INFO     success: api entered RUNNING state
INFO     success: worker entered RUNNING state
INFO:     Uvicorn running on http://0.0.0.0:8000
celery_worker_ready
```

**Verify endpoints:**
```bash
curl http://localhost:8000/ready    # should return {"status":"ready"}
curl http://localhost:8000/health   # deeper diagnostics including celery_worker
```

---

## What You Must Not Do

1. **Do not use `.venv/bin/` in Docker commands.** Packages install globally at `/usr/local/bin/`. The `.venv` prefix is only for native Render scripts.

2. **Do not use honcho inside Docker.** supervisord is the correct process manager for containers.

3. **Do not add a `[program:migrate]` block to supervisord.conf.** Migrations run once in the entrypoint before supervisord starts.

4. **Do not touch `docker/docker-compose.yml` or `docker/docker-compose.test.yml`.** Local dev/test only.

5. **Do not add `--workers 2` to uvicorn.** One worker on 512MB shared with Celery.

6. **Do not change `--pool=solo --concurrency=1` on Celery.** Required for memory constraints.

7. **Do not write `%h` for Celery hostname in supervisord.conf.** Must be `%%h` (supervisord escapes `%`).

8. **Do not move Dockerfile from `docker/Dockerfile`.** Render is configured for this path.

9. **Do not use wrong env var names** (`ENVIRONMENT`, `S3_*`, `MAIL_*`). Use names from `app/config.py`.

---

## App-Level Optimizations (Already Applied)

| Change | File | Why |
|---|---|---|
| Parallel PD + LGD dispatch | `app/modules/runs/router.py` | Reduces execute endpoint latency (~5s → ~2.5s on Upstash TLS) |
| `del files; gc.collect()` after Excel combine | `app/tasks/compute_tasks.py` | Frees raw Excel bytes during PD/LGD compute on 512MB container |
| Lower DB pool guidance | `.env.example` | `DATABASE_POOL_SIZE=3` recommended for small Docker instances |

---

## Post-Deploy Checklist

- [ ] Container logs show both `Uvicorn running` and `celery_worker_ready`
- [ ] `GET /ready` returns 200
- [ ] `GET /health` shows `celery_worker: "ok"` (not `no_workers`)
- [ ] Execute a test run end-to-end from the Vercel frontend
- [ ] Separate Render worker service suspended/deleted if it existed
- [ ] CORS_ORIGINS includes the Vercel production URL
- [ ] Monitor memory during a representative compute job

---

## Known Risks

- **Memory pressure:** Excel uploads and compute tasks fully materialize files in memory. A 512MB container running API + worker can OOM on large uploads. Monitor RSS during compute jobs.
- **Single worker queue:** Compute, email outbox, and periodic cleanup share one Celery worker. Long compute jobs delay email and cleanup tasks.
- **Partial dispatch:** If one of two parallel `.delay()` calls fails, the run is reverted but a task may already be in the broker. `recover_stuck_runs` handles orphaned runs every 5 minutes.
- **`/health` and worker liveness:** `/health` treats `no_workers` as non-degraded. Use log presence of `celery_worker_ready` or manual `/health` inspection to confirm worker is alive.
