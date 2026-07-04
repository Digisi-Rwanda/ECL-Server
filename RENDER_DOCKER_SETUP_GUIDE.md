# ECL-Server — Render Docker Configuration Guide

This guide walks you through every field, every setting, and every environment variable you need to configure on Render so that the ECL-Server runs correctly using Docker with the API and Celery worker running simultaneously.

---

## What Your Current Render Configuration Shows (and What Is Wrong)

From your screenshots, your service is currently configured as a **Native Python** service. This is the wrong mode. You can see this because:

- **Build Command** shows: `python3 -m venv .venv && .venv/bin/pip install --upgrade pip && .venv/bin/pip install -e .`
  → This is the Python virtual environment setup. Docker does not use this.
- **Start Command** shows: `bash scripts/start_production.sh`
  → This runs the API only — no Celery worker.

You need to switch this service to **Docker** runtime. When you do, the Build Command and Start Command fields disappear and are replaced by two fields: `Dockerfile Path` and `Docker Context Directory`. The Docker image handles everything — building, migrations, starting uvicorn AND celery together.

---

## Step 1 — Find the Language/Runtime Setting

In your Render dashboard for the ECL-Server service:

1. Click **Settings** in the left sidebar
2. Scroll to the **Build** section
3. Look for a field called **Language** or **Runtime** at the top of the Build section (it may be above the Source field you can see in your screenshots)

You should see it currently set to **Python 3** or similar. Click the **Edit** button next to it.

> If you cannot find a Language/Runtime field, it means Render may require you to create a new service. Skip to the section **"If You Cannot Change Runtime: Create a New Service"** at the bottom of this guide.

---

## Step 2 — Change Runtime to Docker

Click **Edit** next to the Language field and change the value to **Docker**.

After saving, the Build section will change. The old fields (Build Command, Start Command) will be **replaced** with new Docker-specific fields.

---

## Step 3 — Configure the Docker Build Fields

After switching to Docker, fill in the following fields exactly as shown:

### Build Section

| Field | Value | Why |
|---|---|---|
| **Language / Runtime** | `Docker` | Tells Render to use your Dockerfile instead of native Python |
| **Dockerfile Path** | `docker/Dockerfile` | Your Dockerfile is not in the repo root — it lives in the `docker/` folder |
| **Docker Context Directory** | `.` (a single dot) | Render builds the Docker image from the repo root, so all your code is accessible |
| **Root Directory** | *(leave blank)* | Not needed for Docker |
| **Build Command** | *(leave blank / clear it)* | Docker handles building — no separate build command needed |

### Deploy Section

| Field | Value | Why |
|---|---|---|
| **Pre-Deploy Command** | *(leave blank)* | Migrations run inside the container at startup via `docker_entrypoint.sh` |
| **Start Command** | *(leave blank / clear it)* | The CMD in the Dockerfile runs `scripts/docker_entrypoint.sh` automatically |

> **Critical:** Both Build Command and Start Command must be empty. If there is anything in the Start Command field (like the old `bash scripts/start_production.sh`), Render will try to run that command on the container instead of the CMD from the Dockerfile. Clear it.

---

## Step 4 — Configure the Health Check

Scroll to the **Health & Alerts** section in Settings and set:

| Field | Value |
|---|---|
| **Health Check Path** | `/health` |

This is the endpoint built into your FastAPI app at `app/main.py`. After each deploy, Render will call `GET /health` to confirm the API is up before routing traffic to the new container. If this returns 200, the deploy is marked successful.

---

## Step 5 — Environment Variables (Complete List)

Go to the **Environment** tab in the left sidebar. This is where you need to verify and add variables.

From your screenshots, you already have: `APP_ENV`, `CORS_ORIGINS`, `CSRF_SECRET`, `DATABASE_URL`, `FRONTEND_URL`, `IP_HASH_SALT`, `JWT_PRIVATE_KEY`, `JWT_PUBLIC_KEY`, `LOG_FORMAT`.

Click **Show More** to see the rest. Below is the complete list of every variable the application reads from `app/config.py`, with the correct production value for each:

---

### Group 1 — Application Core

| Variable | Required? | Production Value |
|---|---|---|
| `APP_ENV` | YES | `production` |
| `SECRET_KEY` | YES | A random string of at least 32 characters. Generate with: `openssl rand -hex 32` |
| `LOG_FORMAT` | recommended | `json` (for Render's log viewer) |
| `LOG_LEVEL` | optional | `INFO` |
| `RELEASE` | optional | Your current version, e.g. `1.0.0` |
| `DEBUG` | optional | `false` |

---

### Group 2 — Database

| Variable | Required? | Production Value |
|---|---|---|
| `DATABASE_URL` | YES | Your Neon PostgreSQL connection string. Format: `postgresql+asyncpg://user:password@host/dbname?ssl=require` |

---

### Group 3 — Redis (Two Separate Connections)

Both must be set. They can point to the same Upstash Redis instance using different database numbers, or two separate instances.

| Variable | Required? | Production Value |
|---|---|---|
| `REDIS_URL` | YES | Your Upstash Redis URL for cache and JWT blacklist. Format: `rediss://default:password@host:port` |
| `REDIS_CELERY_URL` | YES | Your Upstash Redis URL for the Celery task queue. Format: `rediss://default:password@host:port/1` |

> **Note on `rediss://`** — The double `s` means TLS. Upstash requires TLS. If your URL starts with `redis://` (single s), change it to `rediss://`.

---

### Group 4 — Authentication (JWT)

| Variable | Required? | Production Value |
|---|---|---|
| `JWT_PRIVATE_KEY` | YES | Your RSA private key in PEM format (the full key including `-----BEGIN RSA PRIVATE KEY-----` header) |
| `JWT_PUBLIC_KEY` | YES | Your RSA public key in PEM format |
| `JWT_ACCESS_TOKEN_EXPIRE_MINUTES` | optional | `15` (default) |
| `JWT_REFRESH_TOKEN_EXPIRE_DAYS` | optional | `7` (default) |
| `TOTP_ENCRYPTION_KEY` | if using 2FA | A 32-byte base64 key for encrypting TOTP secrets |

---

### Group 5 — Security

| Variable | Required? | Production Value |
|---|---|---|
| `CSRF_SECRET` | YES | A random string of at least 32 characters (different from SECRET_KEY) |
| `IP_HASH_SALT` | YES | A random string for hashing IP addresses in audit logs |
| `TRUST_PROXY_HEADERS` | YES — set this! | `true` |
| `TRUSTED_PROXY_COUNT` | YES — set this! | `1` |

> **Why TRUST_PROXY_HEADERS matters:** Render puts a load balancer in front of your service. Without this setting, your app sees Render's internal IP as the client IP, not the real user's IP. Rate limiting and audit logs will malfunction. Set `TRUST_PROXY_HEADERS=true` and `TRUSTED_PROXY_COUNT=1`.

---

### Group 6 — CORS and Frontend

| Variable | Required? | Production Value |
|---|---|---|
| `CORS_ORIGINS` | YES | Your Vercel frontend URL, e.g. `https://your-app.vercel.app` |
| `FRONTEND_URL` | YES | Same as CORS_ORIGINS, e.g. `https://your-app.vercel.app` |

> If you have a custom domain, put that here instead of the Vercel URL.

---

### Group 7 — File Storage (Cloudflare R2)

| Variable | Required? | Production Value |
|---|---|---|
| `STORAGE_ENDPOINT_URL` | YES | Your Cloudflare R2 endpoint URL, e.g. `https://accountid.r2.cloudflarestorage.com` |
| `STORAGE_ACCESS_KEY` | YES | Your R2 access key ID |
| `STORAGE_SECRET_KEY` | YES | Your R2 secret access key |
| `STORAGE_BUCKET_NAME` | YES | Your R2 bucket name, e.g. `ecl-platform` |
| `STORAGE_REGION` | optional | `auto` (for R2) or `us-east-1` |

---

### Group 8 — Email (SMTP)

| Variable | Required? | Production Value |
|---|---|---|
| `SMTP_HOST` | YES | Your email provider's SMTP server, e.g. `smtp.resend.com` or `smtp.gmail.com` |
| `SMTP_PORT` | YES | `587` (TLS) or `465` (SSL) |
| `SMTP_USERNAME` | YES | Your SMTP username |
| `SMTP_PASSWORD` | YES | Your SMTP password or API key |
| `SMTP_FROM_EMAIL` | YES | The "From" address, e.g. `noreply@yourdomain.com` |
| `SMTP_FROM_NAME` | optional | `ECL Platform` |
| `SMTP_TLS` | optional | `true` |
| `SUPPRESS_EMAIL_SEND` | optional | `false` (set `true` only for testing) |

---

### Group 9 — Monitoring (Optional)

| Variable | Required? | Production Value |
|---|---|---|
| `SENTRY_DSN` | optional | Your Sentry project DSN for error tracking |
| `METRICS_TOKEN` | optional | A secret token to protect the `/metrics` endpoint |

---

### Quick Checklist — Variables You Must Not Leave as Default

The app will **refuse to start** in production if any of these still point to localhost or use insecure development defaults:

- [ ] `REDIS_URL` — must NOT be `redis://localhost:6379/0`
- [ ] `REDIS_CELERY_URL` — must NOT be `redis://localhost:6379/1`
- [ ] `CORS_ORIGINS` — must NOT be `http://localhost:3000`
- [ ] `FRONTEND_URL` — must NOT be `http://localhost:3000`
- [ ] `SMTP_HOST` — must NOT be `localhost`
- [ ] `STORAGE_ENDPOINT_URL` — must NOT be `http://localhost:9000`
- [ ] `CSRF_SECRET` — must NOT be `changeme-csrf-secret-32-chars-min`
- [ ] `IP_HASH_SALT` — must NOT be `ecl-ip-hash-salt`
- [ ] `STORAGE_ACCESS_KEY` — must NOT be `minioadmin`
- [ ] `STORAGE_SECRET_KEY` — must NOT be `minioadmin`

These checks are enforced by the `_validate_production_config` validator in `app/config.py`. If any of these are wrong, the container will crash at startup with a clear error message in the logs.

---

## Step 6 — Auto-Deploy Setting

In your Deploy section, **Auto-Deploy** is currently set to **On Commit**. This means every push to the `main` branch automatically triggers a new Docker build and deploy.

Keep this setting as **On Commit** for now. Later, when the compute pipeline is stable and you have active users, you may want to switch to **Manual** deploys so you can choose when to redeploy (and avoid killing an in-progress compute job).

---

## Step 7 — Save Changes and Deploy

1. After filling in all the fields, click **Save Changes** at the bottom of the Settings page
2. Go to the **Deploys** tab or click the **Manual Deploy** button
3. Select **Deploy latest commit**
4. Render will now build your Docker image and start the container

---

## Step 8 — How to Read the Logs After Deploy

Go to the **Logs** tab on your service. A successful Docker deploy will show these lines in order:

```
==> Building Docker image...
Step 1/X : FROM python:3.12-slim AS builder
...
Successfully built <image-id>

==> Starting service...

==> Running database migrations...
INFO  [alembic.runtime.migration] Context impl PostgreSQLImpl.
INFO  [alembic.runtime.migration] Running upgrade ... -> ...
==> Migrations complete.
==> Starting supervisord (api + worker)...
INFO     supervisord started with pid 7
INFO     spawned: 'api' with pid 8
INFO     spawned: 'worker' with pid 9
INFO     success: api entered RUNNING state
INFO     success: worker entered RUNNING state
INFO:     Uvicorn running on http://0.0.0.0:10000
{"event": "celery_worker_ready", "logger": "celery.worker", ...}
```

**The two lines that confirm everything is working:**
- `Uvicorn running on http://0.0.0.0:...` → API is running
- `"event": "celery_worker_ready"` → Celery worker is running

Both appear in the **same log stream** because both processes run inside the same Docker container. This is the proof that supervisord is running both simultaneously.

---

## Step 9 — Test the Deployed API

After the deploy is marked successful, test the API:

**Health check:**
```
GET https://your-render-url.onrender.com/health
```
Should return: `200 OK` with a JSON body

**If you get a 503 or connection refused:** Check the logs for startup errors. Common causes:
- Missing or incorrect environment variable (the app prints which one)
- Database migration failure (check DATABASE_URL value)
- Port misconfiguration (Render provides PORT automatically — you don't set it)

---

## If You Cannot Change Runtime: Create a New Service

If Render does not allow switching the runtime of your existing service from Python to Docker, you need to create a new Web Service. Here is how:

1. Go to **dashboard.render.com**
2. Click **New +** in the top right
3. Select **Web Service**
4. Select **Build and deploy from a Git repository** → click **Next**
5. Search for and connect to `mugisham37/ECL-Server`
6. On the configuration page, look for **Language** — change it from `Python` to **Docker**
7. Fill in:
   - **Name:** `ECL-Server` (or any name)
   - **Region:** Virginia (US East) — same as before
   - **Branch:** `main`
   - **Dockerfile Path:** `docker/Dockerfile`
   - **Docker Context Directory:** `.`
   - **Instance Type:** Free (or upgrade if available)
8. Click **Advanced** → Add all environment variables from Step 5 above
9. Click **Create Web Service**

After the new service is created and running correctly, you can delete the old native Python service.

---

## Summary of Exact Field Values

For quick reference:

| Field | Exact Value |
|---|---|
| Runtime/Language | `Docker` |
| Dockerfile Path | `docker/Dockerfile` |
| Docker Context Directory | `.` |
| Build Command | *(empty)* |
| Start Command | *(empty)* |
| Pre-Deploy Command | *(empty)* |
| Health Check Path | `/health` |
| Auto-Deploy | `On Commit` |
| Branch | `main` |
| Root Directory | *(empty)* |

These are the only fields that are different from a native Python service. Everything else (region, instance type, custom domains, environment variables) stays the same.
