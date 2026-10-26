"""Vercel Queues subscriber for the durable Turso delivery outboxes."""

from __future__ import annotations

import asyncio
import os

from vercel.queue import RetryAfter, subscribe

from delivery_queue import DELIVERY_SIGNAL, DELIVERY_TOPIC


def _next_due_delay() -> int | None:
    """Find the next eligible outbox item without moving any data into Queues."""
    from app import _backend
    from backend import ApiError, normalize_email, now
    from mailer import delivery_configured
    from sheets_sync import sheet_configured

    current = now()
    due_times: list[int] = []
    admin_email = os.getenv("PH_ADMIN_NOTIFICATION_EMAIL", "").strip()
    if admin_email:
        try:
            admin_email = normalize_email(admin_email)
        except ApiError:
            admin_email = ""
    with _backend().connect() as conn:
        if sheet_configured():
            row = conn.execute(
                "SELECT MIN(CASE WHEN lease_until>? THEN lease_until "
                "ELSE next_attempt_at END) AS due_at "
                "FROM sheet_sync_state WHERE revision>synced_revision",
                (current,),
            ).fetchone()
            if row and row["due_at"] is not None:
                due_times.append(int(row["due_at"]))
        if delivery_configured():
            row = conn.execute(
                "SELECT MIN(CASE WHEN status='sending' AND lease_until>? "
                "THEN lease_until ELSE next_attempt_at END) AS due_at "
                "FROM notification_outbox WHERE status IN ('pending','sending') "
                "AND (recipient_kind='user' OR ?=1)",
                (current, bool(admin_email)),
            ).fetchone()
            if row and row["due_at"] is not None:
                due_times.append(int(row["due_at"]))
    if not due_times:
        return None
    # Return promptly for backlog; respect the database's exponential backoff
    # when a provider is unavailable.  Clamp long waits so configuration
    # changes can take effect without waiting for the daily reconciliation.
    return max(1, min(3600, min(due_times) - current + 1))


def _drain_committed_outboxes() -> int | None:
    from app import _dispatch_notifications, _dispatch_sheets
    from backend import SHEET_DATASETS

    failed = False
    try:
        _dispatch_sheets(limit=len(SHEET_DATASETS))
    except Exception:
        failed = True
    try:
        _dispatch_notifications(limit=5)
    except Exception:
        failed = True
    if failed:
        # Do not leak provider exceptions, row data, or recipient addresses to
        # platform logs.  An exception asks Vercel Queues to retry this signal.
        raise RuntimeError("delivery_outbox_unavailable")
    return _next_due_delay()


@subscribe(topic=DELIVERY_TOPIC, retry_after=30)
async def drain_delivery_signal(message: dict[str, object]) -> None:
    """Drain bounded committed work; retry until eligible outboxes are empty."""
    if message != DELIVERY_SIGNAL:
        return
    delay = await asyncio.to_thread(_drain_committed_outboxes)
    if delay is not None:
        raise RetryAfter(delay)
