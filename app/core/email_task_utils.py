"""Shared helpers for Celery email tasks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jinja2 import TemplateError

if TYPE_CHECKING:
    from celery.app.task import Task


def _http_status(exc: BaseException) -> int | None:
    """Extract an HTTP status code from an exception that carries one."""
    code = getattr(exc, "status_code", None)
    return code if isinstance(code, int) else None


def is_transient_email_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    code = _http_status(exc)
    # 429 = rate limit; 5xx = Resend server-side error — both are worth retrying
    if code is not None and (code == 429 or code >= 500):
        return True
    cause = exc.__cause__
    if cause is not None:
        if isinstance(cause, (TimeoutError, ConnectionError, OSError)):
            return True
        c = _http_status(cause)
        if c is not None and (c == 429 or c >= 500):
            return True
    return False


def is_auth_email_error(exc: BaseException) -> bool:
    # 401 = invalid API key; 403 = unverified sending domain
    code = _http_status(exc)
    if code in (401, 403):
        return True
    cause = exc.__cause__
    return cause is not None and _http_status(cause) in (401, 403)


def handle_email_task_exception(
    task: Task[Any, Any],
    exc: BaseException,
    *,
    task_name: str,
    **log_context: object,
) -> None:
    from app.core.logging import get_logger

    log = get_logger("email")

    if isinstance(exc, TemplateError):
        log.error(
            "email_send_failed",
            task=task_name,
            category="template",
            exc=str(exc),
            exc_info=True,
            **log_context,
        )
        return

    if is_auth_email_error(exc):
        log.error(
            "email_send_failed",
            task=task_name,
            category="auth",
            exc=str(exc),
            exc_info=True,
            **log_context,
        )
        raise exc

    if is_transient_email_error(exc):
        retries = task.request.retries
        log.warning(
            "email_send_retry",
            task=task_name,
            category="transient",
            retries=retries,
            exc=str(exc),
            exc_info=True,
            **log_context,
        )
        raise task.retry(countdown=30, max_retries=3, exc=exc)

    log.error(
        "email_send_failed",
        task=task_name,
        category="unknown",
        exc=str(exc),
        exc_info=True,
        **log_context,
    )
    raise exc
