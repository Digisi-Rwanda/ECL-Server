"""Diagnose email delivery: config, Resend domain status, outbox backlog, live test send.

Usage: .venv/bin/python scripts/email_doctor.py you@example.com
"""

import asyncio
import sys

import resend
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.core.email import send_email
from app.modules.email_outbox.models import EmailOutbox


async def main(to: str) -> None:
    s = get_settings()
    key = s.resend_api_key
    sender_domain = s.smtp_from_email.split("@")[-1]
    print(f"RESEND_API_KEY      : {'set (' + key[:5] + '…)' if key else 'MISSING'}")
    print(f"FROM                : {s.smtp_from_name} <{s.smtp_from_email}>")
    print(f"SUPPRESS_EMAIL_SEND : {s.suppress_email_send}")
    print(f"FRONTEND_URL        : {s.frontend_url}")

    resend.api_key = key
    try:
        domains = resend.Domains.list()
        rows = domains["data"] if isinstance(domains, dict) else domains
        status = {d["name"]: d["status"] for d in rows}
        print(f"Resend domains      : {status}")
        if status.get(sender_domain) != "verified":
            print(f"!! {sender_domain} is not verified in this Resend account — sends will 403")
    except Exception as exc:  # sending-only keys can't list domains
        print(f"Resend domains      : could not list ({exc})")

    engine = create_async_engine(s.database_url, poolclass=NullPool)
    async with AsyncSession(engine) as db:
        counts = dict(
            (await db.execute(
                select(EmailOutbox.status, func.count()).group_by(EmailOutbox.status)
            )).all()
        )
    await engine.dispose()
    print(f"email_outbox        : {counts or 'empty'}")
    if counts.get("pending"):
        print("!! pending rows = no Celery worker WITH --beat is running")

    if s.suppress_email_send:
        print("!! SUPPRESS_EMAIL_SEND=true — skipping live send")
        return
    await send_email(
        to=to,
        subject="ECL Platform email test",
        template_name="verify_email.html",
        context={"user_name": "Test", "verify_url": f"{s.frontend_url}/verify-email?token=test"},
    )
    print(f"Live send to {to}   : OK (check Resend → Logs)")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
