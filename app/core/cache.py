from datetime import UTC, datetime

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.redis_ssl import build_redis_connection_kwargs

_redis: Redis | None = None


async def get_redis_client() -> Redis:
    global _redis
    if _redis is None:
        settings = get_settings()
        _redis = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            **build_redis_connection_kwargs(settings.redis_url),
        )
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


async def blacklist_token(
    redis: Redis,
    db: AsyncSession,
    jti: str,
    user_id: str,
    expires_at: datetime,
    reason: str,
) -> None:
    """Blacklist a JWT JTI in Redis (fast) and PostgreSQL (persistent fallback)."""
    from app.modules.auth.models import TokenBlacklist

    ttl = max(1, int((expires_at - datetime.now(UTC)).total_seconds()))
    try:
        await redis.setex(f"blacklist:jti:{jti}", ttl, reason)
    except Exception:
        # Redis unavailable: still write the PostgreSQL row below so the
        # blacklist entry isn't lost — is_token_blacklisted() falls back to
        # the DB when Redis can't be reached.
        from app.core.logging import get_logger

        get_logger("app.cache").warning("blacklist_token_redis_unavailable", jti=jti)

    entry = TokenBlacklist(jti=jti, user_id=user_id, expires_at=expires_at, reason=reason)
    db.add(entry)


async def is_token_blacklisted(
    redis: Redis,
    jti: str,
    db: AsyncSession | None = None,
) -> bool:
    """Check Redis first; fall back to DB if Redis is unavailable."""
    try:
        if await redis.exists(f"blacklist:jti:{jti}") > 0:
            return True
    except Exception:
        pass  # Redis unavailable — fall through to DB

    if db is not None:
        from app.modules.auth.models import TokenBlacklist

        result = await db.execute(
            select(TokenBlacklist).where(
                TokenBlacklist.jti == jti,
                TokenBlacklist.expires_at > datetime.now(UTC),
            )
        )
        return result.scalar_one_or_none() is not None

    return False
