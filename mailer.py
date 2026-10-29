"""Send privacy-preserving transactional notifications through Gmail or Resend.

Only the sender and recipient addresses leave Physics Plus; the subject and message are
selected from fixed templates and contain no student, guardian, timetable, or financial details. A
successful response means the selected provider accepted the message, not
that it reached the recipient's inbox. This module never sends without a
complete provider configuration and never logs credentials or response bodies.

Caller contract: send_update_email(recipient_email, event_id=<stable DB event id>)
returns MailResult. Reuse the same event id when retrying the *same* notice.
Resend's idempotency retention is 24 hours. SMTP has no equivalent guarantee:
an ambiguous SMTP failure may result in a duplicate generic notice on retry.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import smtplib
import ssl
from contextlib import suppress
from dataclasses import dataclass
from email.message import EmailMessage


_API_HOST = "api.resend.com"
_API_PATH = "/emails"
_TIMEOUT_SECONDS = 5.0
_SUBJECT = "Physics Plus: มีข้อมูลอัปเดตในระบบ"
_MESSAGE = (
    "มีข้อมูลอัปเดตในระบบ Physics Plus\n"
    "กรุณาเข้าสู่เว็บไซต์ Physics Plus ด้วยตนเองเพื่อตรวจสอบรายละเอียด\n"
    "หากไม่ได้คาดว่าจะได้รับอีเมลนี้ โปรดละเลยข้อความนี้"
)
_CLASS_CANCELLED_SUBJECT = "Physics Plus: มีการยกคลาสเรียน"
_CLASS_CANCELLED_MESSAGE = (
    "คาบเรียนในตารางของคุณมีการยกคลาส\n"
    "กรุณาเข้าสู่เว็บไซต์ Physics Plus ด้วยตนเองเพื่อตรวจสอบคาบเรียนและเหตุผล\n"
    "หากไม่ได้คาดว่าจะได้รับอีเมลนี้ โปรดละเลยข้อความนี้"
)
_EMAIL_TEST_SUBJECT = "Physics Plus: ทดสอบระบบแจ้งเตือนทางอีเมล"
_EMAIL_TEST_MESSAGE = (
    "นี่คืออีเมลทดสอบจากระบบ Physics Plus\n"
    "ระบบส่งข้อความนี้ตามคำสั่งทดสอบของผู้ดูแล\n"
    "หากไม่ได้สั่งทดสอบ โปรดเข้าสู่ระบบเพื่อตรวจสอบการใช้งาน"
)
_LOCAL_RE = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+\Z", re.ASCII)
_DOMAIN_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z", re.ASCII)
_API_KEY_RE = re.compile(r"re_[A-Za-z0-9_-]{8,256}\Z", re.ASCII)
_GMAIL_HOST = "smtp.gmail.com"
_GMAIL_PORT = 465


@dataclass(frozen=True)
class MailResult:
    """Result suitable for an outbox: `sent` means provider-accepted only."""

    sent: bool
    code: str
    retryable: bool = False
    provider_id: str | None = None
    status: int | None = None
    retry_after_seconds: int | None = None


def _email_address(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 254 or not value.isascii():
        return None
    value = value.strip()
    if value.count("@") != 1:
        return None
    local, domain = value.split("@")
    if not (1 <= len(local) <= 64) or not _LOCAL_RE.fullmatch(local):
        return None
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    labels = domain.split(".")
    if len(labels) < 2 or any(not _DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        return None
    if len(labels[-1]) < 2:
        return None
    return f"{local}@{domain.lower()}"


def _retry_after(value: str | None) -> int | None:
    if value is None or not value.isdecimal() or len(value) > 4:
        return None
    return min(int(value), 3600)


def email_mode() -> str:
    """Return the active delivery mode; unknown values fail closed to inbox only."""

    configured = os.getenv("PH_EMAIL_MODE")
    return "email" if configured is None or configured.strip().lower() == "email" else "inbox_only"


def delivery_configured() -> bool:
    """Return whether a complete server-side provider pair is present."""

    return email_mode() == "email" and bool(
        (os.getenv("PH_GMAIL_USER") and os.getenv("PH_GMAIL_APP_PASSWORD"))
        or (os.getenv("RESEND_API_KEY") and os.getenv("PH_EMAIL_FROM"))
    )


def _smtp_refusal(status: int | None, *, auth: bool = False) -> MailResult:
    if status is not None and 400 <= status < 500:
        return MailResult(False, "temporary_failure", retryable=True, status=status)
    return MailResult(False, "gmail_auth_rejected" if auth else "provider_rejected", status=status)


def _send_gmail_email(recipient: str, sender_value: str, app_password_value: str,
                      *, subject: str = _SUBJECT, body: str = _MESSAGE) -> MailResult:
    sender = _email_address(sender_value)
    # Google displays app passwords in groups. Copying them can also include
    # line breaks or tabs; remove whitespace before the strict 16-character
    # ASCII check so none of it reaches the SMTP transport.
    app_password = re.sub(r"\s+", "", app_password_value)
    if sender is None or sender.split("@", 1)[1] not in {"gmail.com", "googlemail.com"}:
        return MailResult(False, "invalid_gmail_sender")
    if not re.fullmatch(r"[A-Za-z0-9]{16}", app_password, re.ASCII):
        return MailResult(False, "invalid_app_password_format")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    connection = None
    try:
        connection = smtplib.SMTP_SSL(
            _GMAIL_HOST, _GMAIL_PORT, timeout=_TIMEOUT_SECONDS,
            context=ssl.create_default_context(),
        )
        connection.login(sender, app_password)
        refused = connection.send_message(message, from_addr=sender, to_addrs=[recipient])
        if refused:
            code = next(iter(refused.values()))[0]
            return _smtp_refusal(code if isinstance(code, int) else None)
        # Accepted by Gmail's SMTP server, not guaranteed delivered to inbox.
        return MailResult(True, "accepted")
    except smtplib.SMTPAuthenticationError as exc:
        return _smtp_refusal(exc.smtp_code if isinstance(exc.smtp_code, int) else None, auth=True)
    except smtplib.SMTPResponseException as exc:
        return _smtp_refusal(exc.smtp_code if isinstance(exc.smtp_code, int) else None)
    except smtplib.SMTPRecipientsRefused as exc:
        first = next(iter(exc.recipients.values()), (None,))
        code = first[0] if isinstance(first, tuple) and first else None
        return _smtp_refusal(code if isinstance(code, int) else None)
    except ssl.SSLCertVerificationError:
        return MailResult(False, "smtp_tls_failure")
    except (smtplib.SMTPException, OSError, TimeoutError):
        # Failure after DATA may be ambiguous: the outbox may retry this
        # generic notice, but no student or payment details are in the body.
        return MailResult(False, "temporary_failure", retryable=True)
    finally:
        if connection is not None:
            # Closing the socket cannot change a successful DATA response.
            with suppress(OSError, smtplib.SMTPException):
                connection.close()


def send_update_email(recipient_email: str, *, event_id: str,
                      event_type: str | None = None) -> MailResult:
    """Ask the selected provider to email one committed database update.

    `event_id` must uniquely identify this notification. Supply the same ID
    on retries. The function makes one provider attempt and returns a
    non-secret status that a caller can persist or use to schedule a retry.
    It must be called after committing the business transaction; email
    failure must not undo the saved update.
    """

    if email_mode() == "inbox_only":
        return MailResult(False, "email_disabled")

    recipient = _email_address(recipient_email)
    if recipient is None:
        return MailResult(False, "invalid_recipient")
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 256 or any(
        ord(character) < 32 or ord(character) == 127 for character in event_id
    ):
        return MailResult(False, "invalid_event_id")
    if event_type == "class_cancelled":
        subject, body = _CLASS_CANCELLED_SUBJECT, _CLASS_CANCELLED_MESSAGE
    elif event_type == "email_test":
        subject, body = _EMAIL_TEST_SUBJECT, _EMAIL_TEST_MESSAGE
    else:
        subject, body = _SUBJECT, _MESSAGE

    gmail_user = os.getenv("PH_GMAIL_USER", "")
    gmail_password = os.getenv("PH_GMAIL_APP_PASSWORD", "")
    if gmail_user and gmail_password:
        return _send_gmail_email(recipient, gmail_user, gmail_password,
                                 subject=subject, body=body)

    api_key = os.getenv("RESEND_API_KEY", "")
    sender_value = os.getenv("PH_EMAIL_FROM", "")
    if not api_key or not sender_value:
        return MailResult(False, "not_configured")
    sender = _email_address(sender_value)
    if sender is None or not _API_KEY_RE.fullmatch(api_key):
        return MailResult(False, "invalid_configuration")

    # Hash the database event ID and address so neither appears in an HTTP
    # header. A single event sent to several recipients gets distinct keys.
    unique = f"{event_id}\x00{recipient.casefold()}".encode("utf-8")
    idempotency_key = "pp-" + hashlib.sha256(unique).hexdigest()
    payload = json.dumps(
        {"from": sender, "to": [recipient], "subject": subject, "text": body},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    connection = None
    try:
        connection = http.client.HTTPSConnection(
            _API_HOST, timeout=_TIMEOUT_SECONDS, context=ssl.create_default_context()
        )
        connection.request(
            "POST", _API_PATH, body=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
                "Idempotency-Key": idempotency_key,
            },
        )
        response = connection.getresponse()
        status = response.status
        retry_after = _retry_after(response.getheader("Retry-After"))
        # Never return, print, or log provider response text. Keep reads bounded.
        raw = response.read(4096)
        if 200 <= status < 300:
            try:
                provider_id = json.loads(raw).get("id")
            except (ValueError, AttributeError):
                provider_id = None
            if isinstance(provider_id, str) and 1 <= len(provider_id) <= 128:
                return MailResult(True, "accepted", provider_id=provider_id, status=status)
            return MailResult(False, "ambiguous_response", retryable=True, status=status)
        if status == 429 or status == 408 or status >= 500:
            return MailResult(False, "temporary_failure", retryable=True,
                              status=status, retry_after_seconds=retry_after)
        if status == 409:
            try:
                name = json.loads(raw).get("name")
            except (ValueError, AttributeError):
                name = None
            if name == "concurrent_idempotent_requests":
                return MailResult(False, "temporary_failure", retryable=True, status=status)
        return MailResult(False, "provider_rejected", status=status)
    except (OSError, TimeoutError, http.client.HTTPException):
        return MailResult(False, "temporary_failure", retryable=True)
    finally:
        if connection is not None:
            connection.close()
