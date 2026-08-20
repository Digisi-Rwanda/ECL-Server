from slowapi import Limiter
from slowapi.util import get_remote_address

from app.config import get_settings

_settings = get_settings()


def _storage_uri() -> str:
    if not _settings.rate_limit_enabled:
        return "memory://"
    # In production Redis must be available; fail loudly if it isn't.
    if _settings.is_production:
        return _settings.redis_url
    # In dev/staging, probe Redis synchronously at startup and fall back to
    # in-process memory so a missing local Redis doesn't kill every request.
    try:
        import redis as _r
        c = _r.from_url(_settings.redis_url, socket_connect_timeout=1)
        c.ping()
        c.close()
        return _settings.redis_url
    except Exception:
        return "memory://"


limiter = Limiter(
    key_func=get_remote_address,
    enabled=_settings.rate_limit_enabled,
    storage_uri=_storage_uri(),
    # If the Redis-backed storage is unreachable at request time (e.g. an
    # Upstash outage), fail open instead of 500ing every rate-limited route.
    # Rate limiting is a defense-in-depth control, not core functionality —
    # losing it temporarily during an infra outage is preferable to losing
    # login/logout/etc. entirely.
    swallow_errors=True,
)
