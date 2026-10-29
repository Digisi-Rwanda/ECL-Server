import time

from celery import Celery
from celery.schedules import crontab
from celery.signals import (
    setup_logging,
    task_failure,
    task_postrun,
    task_prerun,
    task_retry,
    worker_ready,
    worker_shutdown,
)

from app.config import get_settings
from app.core.redis_ssl import build_celery_redis_ssl_params

settings = get_settings()

celery_app = Celery(
    "ecl_tasks",
    broker=settings.redis_celery_url,
    backend=settings.redis_celery_url,
    include=["app.tasks.email_tasks", "app.tasks.cleanup_tasks", "app.tasks.compute_tasks"],
)

_celery_ssl_params = build_celery_redis_ssl_params(settings.redis_celery_url)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_always_eager=settings.celery_task_always_eager,
    task_soft_time_limit=300,
    task_time_limit=600,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "expire-invitations": {
            "task": "expire_invitations",
            "schedule": crontab(hour=0, minute=0),
        },
        "expire-password-reset-tokens": {
            "task": "expire_password_reset_tokens",
            "schedule": crontab(hour=0, minute=30),
        },
        "purge-revoked-refresh-tokens": {
            "task": "purge_revoked_refresh_tokens",
            "schedule": crontab(hour=1, minute=0),
        },
        "purge-old-sessions": {
            "task": "purge_old_sessions",
            "schedule": crontab(hour=2, minute=0),
        },
        "purge-expired-token-blacklist": {
            "task": "purge_expired_token_blacklist",
            "schedule": crontab(minute="*/30"),
        },
        "process-email-outbox": {
            "task": "process_email_outbox",
            "schedule": 30.0,
        },
        "recover-stuck-runs": {
            "task": "recover_stuck_runs",
            "schedule": 300.0,  # every 5 minutes
        },
    },
    task_annotations={
        "pd_task": {
            "soft_time_limit": settings.compute_soft_time_limit,
            "time_limit": settings.compute_hard_time_limit,
        },
        "lgd_task": {
            "soft_time_limit": settings.compute_soft_time_limit,
            "time_limit": settings.compute_hard_time_limit,
        },
        "ead_ecl_task": {
            "soft_time_limit": settings.compute_soft_time_limit,
            "time_limit": settings.compute_hard_time_limit,
        },
        "workbooks_task": {
            "soft_time_limit": settings.compute_soft_time_limit,
            "time_limit": settings.compute_hard_time_limit,
        },
        "validation_task": {
            "soft_time_limit": settings.compute_soft_time_limit,
            "time_limit": settings.compute_hard_time_limit,
        },
    },
    **(
        {
            "broker_use_ssl": _celery_ssl_params,
            "redis_backend_use_ssl": _celery_ssl_params,
        }
        if _celery_ssl_params
        else {}
    ),
)

_task_starts: dict[str, float] = {}


@setup_logging.connect
def _configure_celery_logging(**kwargs):  # noqa: ARG001
    from app.core.logging import configure_logging

    configure_logging(get_settings())


@worker_ready.connect
def _on_worker_ready(**kwargs):  # noqa: ARG001
    from app.core.logging import get_logger

    get_logger("celery.worker").info("celery_worker_ready")


@worker_shutdown.connect
def _on_worker_shutdown(**kwargs):  # noqa: ARG001
    from app.core.logging import get_logger

    get_logger("celery.worker").info("celery_worker_shutdown")


@task_prerun.connect
def _on_task_prerun(task_id, task, args, **kwargs):  # noqa: ARG001
    _task_starts[task_id] = time.perf_counter()
    from app.core.logging import get_logger

    get_logger("celery.task").info(
        "task_started",
        task_name=task.name,
        task_id=task_id,
        args_summary=str(args)[:200],
    )


@task_postrun.connect
def _on_task_postrun(task_id, task, state, **kwargs):  # noqa: ARG001
    elapsed_ms = round(
        (time.perf_counter() - _task_starts.pop(task_id, time.perf_counter())) * 1000, 2
    )
    from app.core.logging import get_logger

    get_logger("celery.task").info(
        "task_completed",
        task_name=task.name,
        task_id=task_id,
        state=state,
        elapsed_ms=elapsed_ms,
    )


@task_failure.connect
def _on_task_failure(task_id, exception, **kwargs):  # noqa: ARG001
    elapsed_ms = round(
        (time.perf_counter() - _task_starts.pop(task_id, time.perf_counter())) * 1000, 2
    )
    from app.core.logging import get_logger

    get_logger("celery.task").error(
        "task_failed",
        task_id=task_id,
        exc_type=type(exception).__name__,
        exc_value=str(exception),
        elapsed_ms=elapsed_ms,
        exc_info=exception,
    )


@task_retry.connect
def _on_task_retry(request, reason, **kwargs):  # noqa: ARG001
    from app.core.logging import get_logger

    get_logger("celery.task").warning(
        "task_retry",
        task_id=request.id,
        task_name=request.task,
        reason=str(reason),
        retries=request.retries,
    )
