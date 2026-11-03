"""Alert email delivery.

Default: OutboxNotifier. Messages are saved in the app's outbox and NOT sent.
Optional: SmtpNotifier, enabled only when these environment variables are set:
  COSTLENS_SMTP_HOST, COSTLENS_SMTP_FROM            (required)
  COSTLENS_SMTP_PORT (default 587), COSTLENS_SMTP_USERNAME, COSTLENS_SMTP_PASSWORD
  COSTLENS_SMTP_STARTTLS (default "true")
Works with any SMTP relay, including Amazon SES SMTP endpoints. Credentials are
read from the environment only; they are never stored, logged or shown.
Real delivery has only been tested against a fake SMTP server in the test suite.
"""
from __future__ import annotations

import os
import re
import smtplib
from email.message import EmailMessage
from typing import Protocol

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,24}$")


def valid_email(addr: str) -> bool:
    return bool(EMAIL_RE.match(addr or ""))


def _clean_header(text: str) -> str:
    return re.sub(r"[\r\n]+", " ", text)[:200]


class Notifier(Protocol):
    name: str

    def send(self, recipients: list[str], subject: str, body: str) -> tuple[str, str]: ...


class OutboxNotifier:
    name = "outbox"

    def send(self, recipients, subject, body):
        return "not_sent_no_provider", "Email delivery is not configured. Saved to outbox only."


class SmtpNotifier:
    name = "smtp"

    def __init__(self, host: str, sender: str, port: int = 587, username: str | None = None,
                 password: str | None = None, starttls: bool = True):
        if not valid_email(sender):
            raise ValueError("COSTLENS_SMTP_FROM is not a valid email address")
        self.host, self.sender, self.port = host, sender, port
        self.username, self.password, self.starttls = username, password, starttls

    def send(self, recipients, subject, body):
        recipients = [r for r in recipients if valid_email(r)]
        if not recipients:
            return "failed", "No valid recipients."
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = _clean_header(subject)
        msg.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as s:
                if self.starttls:
                    s.starttls()
                if self.username:
                    s.login(self.username, self.password or "")
                s.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            # Never include credentials; the exception text from smtplib does not contain them.
            return "failed", f"{type(exc).__name__}: {str(exc)[:200]}"
        return "sent", f"Accepted by SMTP server {self.host}"


def get_notifier() -> Notifier:
    host = os.environ.get("COSTLENS_SMTP_HOST", "").strip()
    sender = os.environ.get("COSTLENS_SMTP_FROM", "").strip()
    if not host or not sender:
        return OutboxNotifier()
    return SmtpNotifier(
        host=host, sender=sender, port=int(os.environ.get("COSTLENS_SMTP_PORT", "587")),
        username=os.environ.get("COSTLENS_SMTP_USERNAME") or None,
        password=os.environ.get("COSTLENS_SMTP_PASSWORD") or None,
        starttls=os.environ.get("COSTLENS_SMTP_STARTTLS", "true").lower() != "false")


def describe() -> dict:
    n = get_notifier()
    if isinstance(n, SmtpNotifier):
        return {"provider": "smtp", "configured": True, "detail": f"SMTP via {n.host}:{n.port} from {n.sender}"}
    return {"provider": "outbox", "configured": False,
            "detail": "Email is not configured. Alerts are saved to the outbox and not sent. "
                      "Set COSTLENS_SMTP_HOST and COSTLENS_SMTP_FROM to enable sending."}
