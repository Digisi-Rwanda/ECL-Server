import asyncio
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.tasks.celery_app import celery_app


def _run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


@asynccontextmanager
async def _task_db() -> AsyncGenerator[AsyncSession, None]:
    """NullPool session for Celery cleanup tasks.

    Each asyncio.run() call creates a new event loop. The shared AsyncSessionLocal
    uses a pooled engine whose connections are bound to the loop that created them.
    Reusing those connections in a different loop raises:
        RuntimeError: Future attached to a different loop
    NullPool avoids this entirely — it creates a fresh connection per use and
    closes it immediately after, so there is never a stale loop-bound connection.
    """
    from app.database import _prepare_async_url
    from app.config import get_settings

    url, connect_args = _prepare_async_url(get_settings().database_url)
    engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
    try:
        async with AsyncSession(engine) as session:
            yield session
    finally:
        await engine.dispose()


@celery_app.task(name="expire_invitations")
def expire_invitations() -> int:
    async def _run_async() -> int:
        from datetime import UTC, datetime

        from sqlalchemy import update

        from app.core.enums import InvitationStatus
        from app.modules.invites.models import Invitation

        async with _task_db() as db:
            result = await db.execute(
                update(Invitation)
                .where(
                    Invitation.status == InvitationStatus.PENDING.value,
                    Invitation.expires_at < datetime.now(UTC),
                )
                .values(status=InvitationStatus.EXPIRED.value)
            )
            await db.commit()
            return result.rowcount  # type: ignore[return-value]

    return _run(_run_async())


@celery_app.task(name="expire_password_reset_tokens")
def expire_password_reset_tokens() -> int:
    async def _run_async() -> int:
        from datetime import UTC, datetime

        from sqlalchemy import delete

        from app.modules.auth.models import PasswordResetToken

        async with _task_db() as db:
            result = await db.execute(
                delete(PasswordResetToken).where(
                    PasswordResetToken.expires_at < datetime.now(UTC),
                    PasswordResetToken.used_at.is_(None),
                )
            )
            await db.commit()
            return result.rowcount  # type: ignore[return-value]

    return _run(_run_async())


@celery_app.task(name="purge_revoked_refresh_tokens")
def purge_revoked_refresh_tokens() -> int:
    async def _run_async() -> int:
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import delete

        from app.modules.sessions.models import RefreshToken

        cutoff = datetime.now(UTC) - timedelta(days=30)
        async with _task_db() as db:
            result = await db.execute(
                delete(RefreshToken).where(
                    RefreshToken.is_revoked.is_(True),
                    RefreshToken.expires_at < cutoff,
                )
            )
            await db.commit()
            return result.rowcount  # type: ignore[return-value]

    return _run(_run_async())


@celery_app.task(name="purge_old_sessions")
def purge_old_sessions() -> int:
    async def _run_async() -> int:
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import delete

        from app.modules.sessions.models import Session

        cutoff = datetime.now(UTC) - timedelta(days=90)
        async with _task_db() as db:
            result = await db.execute(
                delete(Session).where(Session.last_active_at < cutoff)
            )
            await db.commit()
            return result.rowcount  # type: ignore[return-value]

    return _run(_run_async())


@celery_app.task(name="purge_expired_token_blacklist")
def purge_expired_token_blacklist() -> int:
    async def _run_async() -> int:
        from datetime import UTC, datetime

        from sqlalchemy import delete

        from app.modules.auth.models import TokenBlacklist

        async with _task_db() as db:
            result = await db.execute(
                delete(TokenBlacklist).where(
                    TokenBlacklist.expires_at < datetime.now(UTC)
                )
            )
            await db.commit()
            return result.rowcount  # type: ignore[return-value]

    return _run(_run_async())


@celery_app.task(name="recover_stuck_runs")
def recover_stuck_runs() -> dict:  # type: ignore[type-arg]
    """Mark runs that are stuck in queued/running states as failed.

    Queued > 10 min → worker never picked it up.
    Running > 45 min → exceeds the hard compute time limit; Celery killed the task
    without updating the DB status.
    """
    async def _run_async() -> dict:  # type: ignore[type-arg]
        from datetime import UTC, datetime, timedelta

        from sqlalchemy import update

        from app.core.logging import get_logger
        from app.modules.runs.models import Run

        log = get_logger("cleanup.stuck_runs")
        now = datetime.now(UTC)
        queued_cutoff = now - timedelta(minutes=10)
        running_cutoff = now - timedelta(minutes=45)

        stuck_queued_msg = "Compute job was not picked up by a worker within the timeout window. Ensure the Celery worker is running and try again."
        stuck_running_msg = "Compute stage exceeded the maximum allowed runtime and was forcibly stopped. Please re-run."

        async with _task_db() as db:
            # Runs stuck in queued
            queued_result = await db.execute(
                update(Run)
                .where(
                    Run.status == "queued",
                    Run.updated_at < queued_cutoff,
                    Run.deleted_at.is_(None),
                )
                .values(
                    status="failed",
                    failure_stage="queued",
                    failure_message=stuck_queued_msg,
                    finished_at=now,
                )
            )
            queued_count: int = queued_result.rowcount  # type: ignore[assignment]

            # Runs stuck in a compute stage
            running_result = await db.execute(
                update(Run)
                .where(
                    Run.status.in_(["pd_running", "lgd_running", "ead_running"]),
                    Run.updated_at < running_cutoff,
                    Run.deleted_at.is_(None),
                )
                .values(
                    status="failed",
                    failure_stage="timeout",
                    failure_message=stuck_running_msg,
                    finished_at=now,
                )
            )
            running_count: int = running_result.rowcount  # type: ignore[assignment]

            await db.commit()

        total = queued_count + running_count
        if total > 0:
            log.warning(
                "stuck_runs_recovered",
                queued_recovered=queued_count,
                running_recovered=running_count,
            )
        else:
            log.debug("stuck_runs_check_idle")

        return {"queued_recovered": queued_count, "running_recovered": running_count}

    return _run(_run_async())


@celery_app.task(name="process_email_outbox")
def process_email_outbox() -> dict:  # type: ignore[type-arg]
    """Dispatch pending outbox rows to Celery. Runs every 30 s via beat.

    Uses NullPool to avoid asyncpg connection reuse across event loops.
    Rows that fail 5 times are moved to dead_letter and surface in /health.
    """
    async def _run_async() -> dict:  # type: ignore[type-arg]
        from datetime import UTC, datetime

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
        from sqlalchemy.pool import NullPool

        from app.config import get_settings
        from app.core.logging import get_logger
        from app.modules.email_outbox.models import EmailOutbox
        from app.tasks.email_tasks import (
            send_invite_email,
            send_password_changed_email,
            send_reset_password_email,
            send_verification_email,
            send_welcome_email,
            send_welcome_to_tenant_email,
        )

        DISPATCH = {
            "send_verification_email": lambda p: send_verification_email.delay(
                p["user_id"], p["raw_token"]
            ),
            "send_reset_password_email": lambda p: send_reset_password_email.delay(
                p["user_id"], p["raw_token"], p.get("ip_address")
            ),
            "send_password_changed_email": lambda p: send_password_changed_email.delay(
                p["user_id"], p.get("ip_address")
            ),
            "send_invite_email": lambda p: send_invite_email.delay(
                p["invitation_id"], p["raw_token"]
            ),
            "send_welcome_email": lambda p: send_welcome_email.delay(
                p["user_id"], p["tenant_id"]
            ),
            "send_welcome_to_tenant_email": lambda p: send_welcome_to_tenant_email.delay(
                p["user_id"], p["tenant_id"]
            ),
        }

        log = get_logger("email.outbox")
        s = get_settings()
        engine = create_async_engine(s.database_url, poolclass=NullPool)
        dispatched = dead_lettered = skipped = 0
        found = 0
        try:
            async with AsyncSession(engine) as db:
                rows = (
                    await db.execute(
                        select(EmailOutbox)
                        .where(EmailOutbox.status == "pending")
                        .order_by(EmailOutbox.created_at)
                        .limit(100)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().all()

                found = len(rows)
                if found:
                    log.info("email_outbox_poll_start", found_pending=found)
                else:
                    log.debug("email_outbox_poll_idle")

                now = datetime.now(UTC)
                for row in rows:
                    row.dispatch_attempts += 1
                    dispatch_fn = DISPATCH.get(row.task_name)
                    if dispatch_fn is None:
                        row.status = "dead_letter"
                        row.last_dispatch_error = f"unknown task_name: {row.task_name!r}"
                        dead_lettered += 1
                        log.error(
                            "email_outbox_unknown_task",
                            outbox_id=row.id,
                            task_name=row.task_name,
                        )
                        continue
                    try:
                        dispatch_fn(row.payload)
                        row.status = "dispatched"
                        row.dispatched_at = now
                        dispatched += 1
                        log.info(
                            "email_outbox_dispatched",
                            outbox_id=row.id,
                            task_name=row.task_name,
                            payload_keys=list(row.payload.keys()),
                        )
                    except Exception as exc:
                        row.last_dispatch_error = str(exc)[:500]
                        if row.dispatch_attempts >= 5:
                            row.status = "dead_letter"
                            dead_lettered += 1
                            log.error(
                                "email_outbox_dead_letter",
                                outbox_id=row.id,
                                task_name=row.task_name,
                                attempts=row.dispatch_attempts,
                                error=str(exc),
                            )
                        else:
                            skipped += 1
                            log.warning(
                                "email_outbox_dispatch_failed",
                                outbox_id=row.id,
                                task_name=row.task_name,
                                attempts=row.dispatch_attempts,
                                error=str(exc),
                            )

                await db.commit()
        finally:
            await engine.dispose()

        if found:
            log.info(
                "email_outbox_poll_done",
                found=found,
                dispatched=dispatched,
                dead_lettered=dead_lettered,
                skipped=skipped,
            )
        return {"found": found, "dispatched": dispatched, "dead_lettered": dead_lettered, "skipped": skipped}

    return _run(_run_async())
