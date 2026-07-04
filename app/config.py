from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEV_DEFAULT_CSRF_SECRET = "changeme-csrf-secret-32-chars-min"
_DEV_DEFAULT_IP_HASH_SALT = "ecl-ip-hash-salt"
_DEV_DEFAULT_STORAGE_ACCESS_KEY = "minioadmin"
_DEV_DEFAULT_STORAGE_SECRET_KEY = "minioadmin"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: Literal["development", "staging", "production"] = "development"
    app_name: str = "ECL Platform API"
    app_version: str = "1.0.0"
    debug: bool = False
    secret_key: str = Field(min_length=32)

    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = 4
    reload: bool = False

    database_url: str
    database_pool_size: int = 10
    database_max_overflow: int = 20
    database_pool_timeout: int = 30

    redis_url: str = "redis://localhost:6379/0"
    redis_celery_url: str = "redis://localhost:6379/1"
    redis_cache_ttl_default: int = 300

    jwt_private_key: str = ""
    jwt_public_key: str = ""
    jwt_key_id: str = "ecl-auth-2026-01"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7
    jwt_algorithm: str = "RS256"

    argon2_memory_cost: int = 65536
    argon2_time_cost: int = 3
    argon2_parallelism: int = 4

    cors_origins: str = "http://localhost:3000"

    smtp_from_email: str = "noreply@eclplatform.com"
    smtp_from_name: str = "ECL Platform"
    resend_api_key: str = ""
    demo_notification_email: str = ""  # if empty, falls back to smtp_from_email

    frontend_url: str = "http://localhost:3000"

    hibp_timeout_seconds: float = 2.0
    hibp_enabled: bool = True

    sentry_dsn: str = ""
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "console"
    release: str = "dev"

    rate_limit_enabled: bool = True

    lockout_threshold_1: int = 5
    lockout_threshold_2: int = 10
    lockout_threshold_3: int = 20

    metrics_token: str = ""

    celery_task_always_eager: bool = False

    refresh_cookie_name: str = "ecl_refresh"

    suppress_email_send: bool = False

    trust_proxy_headers: bool = False
    trusted_proxy_count: int = 1

    csrf_secret: str = _DEV_DEFAULT_CSRF_SECRET

    totp_issuer_name: str = "ECL Platform"
    totp_encryption_key: str = ""

    ip_hash_salt: str = _DEV_DEFAULT_IP_HASH_SALT

    storage_endpoint_url: str = "http://localhost:9000"
    storage_access_key: str = _DEV_DEFAULT_STORAGE_ACCESS_KEY
    storage_secret_key: str = _DEV_DEFAULT_STORAGE_SECRET_KEY
    storage_bucket_name: str = "ecl-platform"
    storage_region: str = "us-east-1"
    max_upload_bytes: int = 52_428_800  # 50 MB

    compute_soft_time_limit: int = 1800
    compute_hard_time_limit: int = 2400

    session_idle_timeout_minutes: int = 30

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors(cls, v: str | list[str]) -> str:
        if isinstance(v, list):
            return ",".join(v)
        return v

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @model_validator(mode="after")
    def _validate_production_config(self) -> "Settings":
        if self.app_env != "production":
            return self

        insecure_defaults = {
            "csrf_secret": _DEV_DEFAULT_CSRF_SECRET,
            "ip_hash_salt": _DEV_DEFAULT_IP_HASH_SALT,
            "storage_access_key": _DEV_DEFAULT_STORAGE_ACCESS_KEY,
            "storage_secret_key": _DEV_DEFAULT_STORAGE_SECRET_KEY,
        }
        localhost_fields = {
            "redis_url": self.redis_url,
            "redis_celery_url": self.redis_celery_url,
            "cors_origins": self.cors_origins,
            "frontend_url": self.frontend_url,
            "storage_endpoint_url": self.storage_endpoint_url,
        }

        errors: list[str] = []
        if not self.resend_api_key and not self.suppress_email_send:
            errors.append(
                "resend_api_key is not set and SUPPRESS_EMAIL_SEND is false; "
                "emails will fail to deliver in production."
            )
        for field_name, dev_value in insecure_defaults.items():
            if getattr(self, field_name) == dev_value:
                errors.append(
                    f"{field_name} is still set to its insecure development default; "
                    "set a real value via the environment in production."
                )
        for field_name, value in localhost_fields.items():
            if "localhost" in value or "127.0.0.1" in value:
                errors.append(
                    f"{field_name} points at localhost ({value!r}); "
                    "set the real production value via the environment."
                )

        if errors:
            raise ValueError(
                "Insecure/development configuration detected with app_env=production:\n  - "
                + "\n  - ".join(errors)
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
