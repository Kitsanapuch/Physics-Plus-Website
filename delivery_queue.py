"""Wake the delivery worker after a committed database change.

The queue carries no student, teacher, payment, or email data.  Turso's sheet
and notification outboxes remain the source of truth; duplicate wake-ups are
safe because their dispatchers lease and acknowledge database rows.
"""

from __future__ import annotations

import os


DELIVERY_TOPIC = "physics-plus-delivery"
DELIVERY_SIGNAL = {"kind": "drain", "v": 1}


def publish_delivery_signal() -> bool:
    """Publish one best-effort wake-up, returning False for local fallback.

    Vercel supplies the queue credentials and deployment partition.  Keep the
    request timeout short: a queue outage must not hold up a successful write.
    No idempotency key is used, since a later write must never lose its wake-up.
    """
    if not os.getenv("VERCEL"):
        return False
    if os.getenv("PH_DELIVERY_QUEUE_ENABLED", "1").strip().lower() in {
        "0", "false", "no", "off",
    }:
        return False
    try:
        from vercel.queue.sync import QueueClient

        QueueClient(timeout=2).send(
            DELIVERY_TOPIC,
            DELIVERY_SIGNAL,
            retention=7 * 24 * 60 * 60,
        )
    except Exception:
        # The already-committed database outboxes are safe to retry via the
        # caller's fallback path and the existing reconciliation crons.
        return False
    return True
