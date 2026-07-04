import base64
import re
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.limiter import limiter
from app.core.middleware import RequestIDMiddleware, SecurityHeadersMiddleware
from app.database import engine


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    from app.core.logging import get_logger

    settings = get_settings()
    log = get_logger("app.lifecycle")

    db_status: str = "ok"
    redis_status: str = "ok"
    try:
        from sqlalchemy import text

        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        db_status = f"error: {exc}"
    try:
        from app.core.cache import get_redis_client

        r = await get_redis_client()
        await r.ping()
    except Exception as exc:
        redis_status = f"error: {exc}"

    storage_status: str = "ok"
    try:
        from app.core.storage import init_storage, get_storage_client

        await init_storage()
        client = await get_storage_client()
        try:
            await client.head_bucket(Bucket=settings.storage_bucket_name)
        except Exception:
            await client.create_bucket(Bucket=settings.storage_bucket_name)
            log.info("storage_bucket_created", bucket=settings.storage_bucket_name)
    except Exception as exc:
        storage_status = f"error: {exc}"

    email_status: str = "ok"
    if not settings.resend_api_key and not settings.suppress_email_send:
        email_status = "misconfigured: RESEND_API_KEY is not set"
        log.warning("email_startup_check_failed", reason="RESEND_API_KEY not set")

    _app.state.smtp_status = email_status

    log.info(
        "server_startup",
        version=settings.app_version,
        env=settings.app_env,
        db=db_status,
        redis=redis_status,
        storage=storage_status,
        smtp=email_status,
        routes=len(_app.routes),
        log_format=settings.log_format,
        log_level=settings.log_level,
    )

    yield

    await engine.dispose()
    from app.core.cache import close_redis

    await close_redis()
    log.info("server_shutdown", reason="lifespan_exit")


def create_app() -> FastAPI:
    settings = get_settings()
    from app.core.logging import configure_logging

    configure_logging(settings)
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        debug=settings.debug,
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

    register_exception_handlers(app)

    app.add_middleware(RequestIDMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-CSRF-Token"],
        expose_headers=["X-Request-ID", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
        max_age=600,
    )

    try:
        from starlette_csrf import CSRFMiddleware

        app.add_middleware(
            CSRFMiddleware,
            secret=settings.csrf_secret,
            sensitive_cookies={settings.refresh_cookie_name},
            cookie_name="csrftoken",
            cookie_path="/",
            cookie_secure=settings.is_production,
            cookie_samesite="lax",
            header_name="x-csrf-token",
            exempt_urls=[
                re.compile(r"^/$"),
                re.compile(r"^/health$"),
                re.compile(r"^/ready$"),
                re.compile(r"^/\.well-known/"),
                # All auth endpoints (login, refresh, etc.)
                re.compile(r"^/api/v1/auth/"),
                # JWT-protected API routes — Bearer tokens are CSRF-immune
                re.compile(r"^/api/v1/tenants/"),
                re.compile(r"^/api/v1/results/"),
                re.compile(r"^/api/v1/platform/"),
                re.compile(r"^/api/v1/segments/"),
                re.compile(r"^/api/v1/collateral/"),
                re.compile(r"^/api/v1/audit/"),
                re.compile(r"^/api/v1/sessions/"),
                re.compile(r"^/api/v1/settings/"),
                re.compile(r"^/api/v1/invites/"),
                re.compile(r"^/api/v1/onboarding/"),
                re.compile(r"^/api/v1/demo"),
                re.compile(r"^/docs"),
                re.compile(r"^/openapi"),
                re.compile(r"^/metrics"),
            ],
        )
    except ImportError:
        pass  # starlette-csrf not installed yet; skips gracefully

    from app.routers import api_router

    app.include_router(api_router)

    try:
        from prometheus_fastapi_instrumentator import Instrumentator

        Instrumentator().instrument(app).expose(
            app,
            endpoint="/metrics",
            include_in_schema=False,
            should_gzip=True,
        )
    except Exception:
        pass  # metrics optional if deps missing

    if settings.sentry_dsn:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration

        sentry_sdk.init(
            dsn=settings.sentry_dsn,
            release=settings.release,
            integrations=[FastApiIntegration()],
        )

    @app.get("/health")
    async def health(request: Request) -> dict[str, object]:
        import asyncio
        from sqlalchemy import func, select, text

        db_ok = "ok"
        redis_ok = "ok"
        storage_ok = "ok"
        smtp_ok: str = getattr(request.app.state, "smtp_status", "ok")
        celery_worker = "ok"
        email_queue_depth: int = 0
        outbox_pending: int = 0
        outbox_dead_letters: int = 0

        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception:
            db_ok = "error"

        try:
            from app.core.cache import get_redis_client
            redis = await get_redis_client()
            await redis.ping()
        except Exception:
            redis_ok = "error"

        try:
            from app.core.storage import get_storage_client
            _sc = await get_storage_client()
            await _sc.head_bucket(Bucket=settings.storage_bucket_name)
        except Exception as exc:
            storage_ok = f"error: {exc}"

        try:
            from app.tasks.celery_app import celery_app as _celery
            pong = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _celery.control.inspect(timeout=5).ping()
            )
            celery_worker = "ok" if pong else "no_workers"
        except Exception as exc:
            celery_worker = f"error: {exc}"

        try:
            import redis.asyncio as _aioredis
            from app.core.redis_ssl import build_redis_connection_kwargs as _build_kwargs
            _r = _aioredis.from_url(
                settings.redis_celery_url,
                **_build_kwargs(settings.redis_celery_url),
            )
            email_queue_depth = int(await _r.llen("celery") or 0)
            await _r.aclose()
        except Exception:
            pass

        try:
            from app.modules.email_outbox.models import EmailOutbox
            async with engine.connect() as conn:
                r1 = await conn.execute(
                    select(func.count()).select_from(EmailOutbox).where(
                        EmailOutbox.status == "pending"
                    )
                )
                outbox_pending = int(r1.scalar() or 0)
                r2 = await conn.execute(
                    select(func.count()).select_from(EmailOutbox).where(
                        EmailOutbox.status == "dead_letter"
                    )
                )
                outbox_dead_letters = int(r2.scalar() or 0)
        except Exception:
            pass

        is_degraded = (
            redis_ok != "ok"
            or storage_ok != "ok"
            or celery_worker not in ("ok", "no_workers")
            or outbox_dead_letters > 0
        )
        return {
            "status": "degraded" if is_degraded else "ok",
            "db": db_ok,
            "redis": redis_ok,
            "storage": storage_ok,
            "smtp": smtp_ok,
            "celery_worker": celery_worker,
            "email_queue_depth": email_queue_depth,
            "email_outbox_pending": outbox_pending,
            "email_outbox_dead_letters": outbox_dead_letters,
            "version": settings.app_version,
        }

    @app.get("/ready", response_model=None)
    async def ready() -> JSONResponse | dict[str, str]:
        try:
            from sqlalchemy import text

            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"status": "ready"}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "not_ready"})

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def root() -> dict[str, str]:
        return {"status": "ok", "version": settings.app_version}

    @app.get("/.well-known/jwks.json", include_in_schema=False)
    async def jwks() -> JSONResponse:
        if not settings.jwt_public_key:
            return JSONResponse(content={"keys": []})
        pem = settings.jwt_public_key
        if not pem.startswith("-----BEGIN"):
            pem = base64.b64decode(pem).decode()
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        key = load_pem_public_key(pem.encode())
        numbers = key.public_numbers()
        n = base64.urlsafe_b64encode(
            numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")
        ).rstrip(b"=").decode()
        e = base64.urlsafe_b64encode(
            numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")
        ).rstrip(b"=").decode()
        return JSONResponse(
            content={
                "keys": [
                    {
                        "kty": "RSA",
                        "use": "sig",
                        "alg": settings.jwt_algorithm,
                        "kid": settings.jwt_key_id,
                        "n": n,
                        "e": e,
                    }
                ]
            },
            headers={"Cache-Control": "max-age=3600"},
        )

    return app


app = create_app()
