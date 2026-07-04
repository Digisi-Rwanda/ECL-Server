"""Email client — Jinja2 rendering + Resend API delivery."""

import asyncio
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

from app.core.logging import get_logger

_log = get_logger("email")

_template_dir = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(str(_template_dir)), autoescape=True)


def render_template(name: str, **context: object) -> str:
    template = _env.get_template(name)
    return template.render(**context)


async def send_email(to: str, subject: str, template_name: str, context: dict) -> None:  # type: ignore[type-arg]
    """Send an HTML email via Resend API.

    Set SUPPRESS_EMAIL_SEND=true in tests to skip actual delivery.
    """
    import resend

    from app.config import get_settings

    s = get_settings()

    _log.info(
        "email_send_start",
        recipient=to,
        subject=subject,
        template=template_name,
        suppress_send=s.suppress_email_send,
    )

    if s.suppress_email_send:
        _log.info(
            "email_send_suppressed",
            recipient=to,
            template=template_name,
            reason="SUPPRESS_EMAIL_SEND=true",
        )
        return

    try:
        html = render_template(template_name, **context)
    except Exception as exc:
        _log.error(
            "email_template_render_failed",
            recipient=to,
            template=template_name,
            exc=str(exc),
            exc_info=True,
        )
        raise

    resend.api_key = s.resend_api_key
    params: resend.Emails.SendParams = {
        "from": f"{s.smtp_from_name} <{s.smtp_from_email}>",
        "to": [to],
        "subject": subject,
        "html": html,
    }

    try:
        result = await asyncio.to_thread(resend.Emails.send, params)
    except Exception as exc:
        _log.error(
            "email_send_failed",
            recipient=to,
            subject=subject,
            template=template_name,
            exc=str(exc),
            exc_info=True,
        )
        raise

    _log.info(
        "email_sent_success",
        recipient=to,
        subject=subject,
        template=template_name,
        resend_id=getattr(result, "id", None),
    )
