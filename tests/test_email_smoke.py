# Email smoke tests.
#
# Mocked tests run in the default suite. Live Resend tests require:
#   SUPPRESS_EMAIL_SEND=false
#   RESEND_API_KEY in .env, RESEND_TEST_RECIPIENT in the environment
#   Run: pytest tests/test_email_smoke.py -m resend -v

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


async def test_render_verify_email_template() -> None:
    from app.core.email import render_template

    html = render_template(
        "verify_email.html",
        user_name="Test User",
        verify_url="http://localhost:3000/verify-email?token=abc123",
    )
    assert "Test User" in html
    assert "http://localhost:3000/verify-email?token=abc123" in html
    assert "Verify Email Address" in html


async def test_send_email_suppressed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import get_settings

    monkeypatch.setenv("SUPPRESS_EMAIL_SEND", "true")
    get_settings.cache_clear()

    from app.core.email import send_email

    settings = get_settings()
    assert settings.suppress_email_send is True

    await send_email(
        to="test@example.com",
        subject="Test",
        template_name="verify_email.html",
        context={
            "user_name": "Test",
            "verify_url": "http://localhost:3000/verify-email?token=x",
        },
    )

    get_settings.cache_clear()


async def test_verification_email_task_eager(monkeypatch: pytest.MonkeyPatch) -> None:
    """Task pipeline with SUPPRESS_EMAIL_SEND — no real send."""
    monkeypatch.setenv("CELERY_TASK_ALWAYS_EAGER", "true")
    monkeypatch.setenv("SUPPRESS_EMAIL_SEND", "true")
    from app.config import get_settings

    get_settings.cache_clear()

    from app.tasks.celery_app import celery_app
    from app.tasks.email_tasks import send_verification_email

    celery_app.conf.task_always_eager = True

    with patch("app.tasks.email_tasks._run") as mock_run:
        mock_run.return_value = (True, "user@example.com")
        send_verification_email("user-id", "raw-token")
        mock_run.assert_called_once()

    get_settings.cache_clear()


async def test_full_dispatch_chain(
    client: AsyncClient, strong_password: str
) -> None:
    """Register → email_outbox row written atomically in the same transaction.

    With the transactional outbox pattern, signup writes a pending row to
    email_outbox instead of directly dispatching a Celery task.  This test
    verifies the row was created with the correct task_name and payload so the
    process_email_outbox poller can dispatch it within 30 seconds.
    """
    from sqlalchemy import select

    from app.modules.email_outbox.models import EmailOutbox
    from tests.conftest import TestSessionLocal

    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "chain-test@example.com",
            "password": strong_password,
            "name": "Chain Test",
            "company_name": "Chain Workspace",
        },
    )
    assert resp.status_code == 201

    async with TestSessionLocal() as db:
        result = await db.execute(
            select(EmailOutbox).where(
                EmailOutbox.task_name == "send_verification_email"
            )
        )
        row = result.scalar_one()

    assert row.status == "pending"
    assert "user_id" in row.payload
    assert "raw_token" in row.payload
    assert row.dispatch_attempts == 0


@pytest.mark.resend
async def test_send_email_live_resend() -> None:
    """Send a real email via Resend — skipped unless -m resend is passed."""
    import os

    if os.environ.get("SUPPRESS_EMAIL_SEND", "false").lower() in ("true", "1", "yes"):
        pytest.skip("SUPPRESS_EMAIL_SEND is enabled")

    recipient = os.environ.get("RESEND_TEST_RECIPIENT")
    if not recipient:
        pytest.skip("Set RESEND_TEST_RECIPIENT to run live Resend test")

    from app.config import get_settings
    from app.core.email import send_email

    settings = get_settings()
    if not settings.resend_api_key:
        pytest.skip("RESEND_API_KEY not configured")

    await send_email(
        to=recipient,
        subject="ECL Platform Resend smoke test",
        template_name="verify_email.html",
        context={
            "user_name": "Smoke Test",
            "verify_url": f"{settings.frontend_url}/verify-email?token=smoke-test",
        },
    )
