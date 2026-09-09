import base64
import os
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DEBUG", "true")
os.environ.setdefault(
    "SECRET_KEY",
    "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
)
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+asyncpg://ecl:ecl_password@localhost:5434/ecl_test_db",
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6380/0")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("HIBP_ENABLED", "false")
os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "true")
os.environ.setdefault("SUPPRESS_EMAIL_SEND", "true")
os.environ.setdefault("CSRF_SECRET", "test-csrf-secret-32-chars-minimum-xx")
os.environ.setdefault("IP_HASH_SALT", "test-ip-hash-salt-for-tests-only-xx")
# Fernet key for TOTP encryption (test-only key generated inline)
import base64 as _b64, secrets as _sec  # noqa: E401, E402

os.environ.setdefault(
    "TOTP_ENCRYPTION_KEY",
    _b64.urlsafe_b64encode(_sec.token_bytes(32)).decode(),
)

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_private_pem = _key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
_public_pem = _key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)
os.environ["JWT_PRIVATE_KEY"] = base64.b64encode(_private_pem).decode()
os.environ["JWT_PUBLIC_KEY"] = base64.b64encode(_public_pem).decode()

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.modules.audit.models import AuditLog  # noqa: E402
from app.modules.auth.models import (  # noqa: E402
    EmailVerificationToken,
    PasswordResetToken,
    TokenBlacklist,
    User,
)
from app.modules.collateral.models import CollateralType  # noqa: E402
from app.modules.invites.models import Invitation  # noqa: E402
from app.modules.results.models import (  # noqa: E402
    EadResult,
    LgdResult,
    OutputArtifact,
    PdObservationSnapshot,
    PdResult,
)
from app.modules.runs.models import PdValidationThresholds, Run, Upload  # noqa: E402
from app.modules.segments.models import Segment  # noqa: E402
from app.modules.sessions.models import RefreshToken, Session  # noqa: E402
from app.modules.tenants.models import Tenant, TenantMembership  # noqa: E402
from app.modules.email_outbox.models import EmailOutbox  # noqa: E402

test_engine = create_async_engine(
    os.environ["DATABASE_URL"],
    echo=False,
    poolclass=NullPool,
)
TestSessionLocal = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

_CLEAN_ORDER = [
    EmailOutbox,
    AuditLog,
    EadResult,
    LgdResult,
    PdResult,
    PdObservationSnapshot,
    OutputArtifact,
    Upload,
    Run,
    PdValidationThresholds,
    TokenBlacklist,
    Session,
    RefreshToken,
    EmailVerificationToken,
    PasswordResetToken,
    Invitation,
    Segment,
    CollateralType,
    TenantMembership,
    Tenant,
    User,
]


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_database() -> AsyncGenerator[None, None]:
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await test_engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_db() -> AsyncGenerator[None, None]:
    async with TestSessionLocal() as session:
        for model in _CLEAN_ORDER:
            await session.execute(delete(model))
        await session.commit()
    yield


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with TestSessionLocal() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture
def strong_password() -> str:
    return "SecurePass123!"
