"""Validation as a background job.

PD, LGD and EAD validation parse whole workbooks and (for EAD) run the full ECL
preview. Doing that inside the HTTP request ties up the API and times out on
big books, so the request only records a job; a worker runs it and stores the
result in Redis, and the browser polls a cheap status endpoint.

One active job per (run, scope): a double click or a retried request joins the
running job instead of starting a second one.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from redis.asyncio import Redis

from app.config import get_settings
from app.core.exceptions import ECLException
from app.core.logging import get_logger

log = get_logger("runs.validation_jobs")

SCOPES = ("pd", "lgd", "ead")
JOB_TTL_SECONDS = 2 * 60 * 60
# An active-job marker older than this is treated as abandoned (worker crash).
ACTIVE_TTL_SECONDS = 30 * 60


def _job_key(job_id: str) -> str:
    return f"validation-job:{job_id}"


def _active_key(run_id: str, scope: str) -> str:
    return f"validation-active:{run_id}:{scope}"


def fresh_redis() -> Redis:
    """A client for the calling event loop (Celery tasks run each job in a new loop)."""
    from app.core.redis_ssl import build_redis_connection_kwargs

    url = get_settings().redis_url
    return Redis.from_url(url, decode_responses=True, **build_redis_connection_kwargs(url))


async def create_job(
    redis: Redis, *, tenant_id: str, run_id: str, scope: str
) -> tuple[str, bool]:
    """Return (job_id, created). Joins the active job for this run and scope if there is one."""
    if scope not in SCOPES:
        raise ECLException("INVALID_SCOPE", "scope must be pd, lgd or ead.", 400)
    job_id = uuid4().hex
    if not await redis.set(_active_key(run_id, scope), job_id, nx=True, ex=ACTIVE_TTL_SECONDS):
        existing = await redis.get(_active_key(run_id, scope))
        if existing and await redis.exists(_job_key(existing)):
            return existing, False
        await redis.set(_active_key(run_id, scope), job_id, ex=ACTIVE_TTL_SECONDS)
    await _write(redis, job_id, {
        "jobId": job_id, "tenantId": tenant_id, "runId": run_id, "scope": scope, "status": "queued",
    })
    return job_id, True


async def _write(redis: Redis, job_id: str, payload: dict[str, Any]) -> None:
    await redis.set(_job_key(job_id), json.dumps(payload, default=str), ex=JOB_TTL_SECONDS)


async def get_job(redis: Redis, *, tenant_id: str, run_id: str, job_id: str) -> dict[str, Any]:
    raw = await redis.get(_job_key(job_id))
    job = json.loads(raw) if raw else None
    if not job or job.get("tenantId") != tenant_id or job.get("runId") != run_id:
        raise ECLException("JOB_NOT_FOUND", "Validation job not found or expired; validate again.", 404)
    return {k: v for k, v in job.items() if k not in ("tenantId",)}


async def run_job(
    redis: Redis,
    session_factory,
    *,
    job_id: str,
    tenant_id: str,
    run_id: str,
    user_id: str,
    scope: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Execute one validation and store its outcome. Never raises."""
    from app.modules.runs import ecl_validation, pd_validation

    run = {
        "pd": pd_validation.validate_pd_files,
        "lgd": ecl_validation.validate_lgd_files,
        "ead": ecl_validation.validate_ead_files,
    }[scope]
    base = {"jobId": job_id, "tenantId": tenant_id, "runId": run_id, "scope": scope}
    await _write(redis, job_id, {**base, "status": "running"})
    try:
        async with session_factory() as db:
            result = await run(db, tenant_id, run_id, user_id, ip=ip, user_agent=user_agent)
            await db.commit()
        await _write(redis, job_id, {**base, "status": "complete", "result": result.model_dump(mode="json")})
        log.info("validation_job_complete", job_id=job_id, run_id=run_id, scope=scope, status=result.status)
    except ECLException as exc:
        await _write(redis, job_id, {**base, "status": "error", "error": {
            "code": exc.code, "detail": exc.detail, "status": exc.status_code}})
        log.info("validation_job_rejected", job_id=job_id, run_id=run_id, scope=scope, code=exc.code)
    except Exception as exc:
        await _write(redis, job_id, {**base, "status": "error", "error": {
            "code": "VALIDATION_FAILED",
            "detail": "Validation failed unexpectedly. Try again; if it repeats, contact support.",
            "status": 500}})
        log.error("validation_job_failed", job_id=job_id, run_id=run_id, scope=scope, exc_info=exc)
    finally:
        if await redis.get(_active_key(run_id, scope)) == job_id:
            await redis.delete(_active_key(run_id, scope))


async def run_job_isolated(**job: Any) -> None:
    """run_job with its own DB engine and Redis client (inline mode, Celery tasks).

    Pooled connections belong to the loop that opened them; a job that outlives
    its request must not borrow them.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from app.database import _connect_args, _db_url

    engine = create_async_engine(_db_url, poolclass=NullPool, connect_args=_connect_args)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    redis = fresh_redis()
    try:
        await run_job(redis, maker, **job)
    finally:
        await redis.aclose()
        await engine.dispose()
