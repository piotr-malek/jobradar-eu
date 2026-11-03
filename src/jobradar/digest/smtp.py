"""SMTP sender (STARTTLS) with attachment support."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from pathlib import Path

from jobradar.settings import Settings, get_settings

log = logging.getLogger(__name__)


class MailError(RuntimeError):
    pass


def verify_config(settings: Settings | None = None) -> None:
    s = settings or get_settings()
    missing = [n for n, v in (("SMTP_USER", s.smtp_user), ("SMTP_PASSWORD", s.smtp_password),
                              ("EMAIL_TO", s.email_to)) if not v]
    if missing:
        raise MailError(f"missing SMTP settings: {', '.join(missing)}")
    pw = s.smtp_password or ""
    if pw[:1] in "\"'" or pw[-1:] in "\"'":
        raise MailError("SMTP_PASSWORD looks quoted — remove the quotes")
    if "gmail" in s.smtp_host and len(pw.replace(" ", "")) != 16:
        raise MailError("Gmail needs a 16-character app password")


def verify_login(settings: Settings | None = None) -> None:
    s = settings or get_settings()
    verify_config(s)
    with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(s.smtp_user, s.smtp_password)


def send(subject: str, html: str, text: str, attachments: list[tuple[str, bytes, str]] | None = None,
         settings: Settings | None = None, fallback_dir: Path | None = None) -> None:
    s = settings or get_settings()
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = s.smtp_user
    msg["To"] = s.email_to
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    for name, data, mime in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    try:
        verify_config(s)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=60) as smtp:
            smtp.starttls()
            smtp.login(s.smtp_user, s.smtp_password)
            smtp.send_message(msg)
    except Exception as exc:
        if fallback_dir:
            fallback_dir.mkdir(parents=True, exist_ok=True)
            (fallback_dir / "unsent_digest.html").write_text(html, encoding="utf-8")
            log.error("send failed (%s); digest saved to %s", type(exc).__name__, fallback_dir)
        raise MailError(f"send failed: {type(exc).__name__}: {exc}") from exc
