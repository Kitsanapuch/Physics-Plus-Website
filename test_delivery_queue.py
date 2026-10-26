"""Queue wake-up tests; no test publishes to Vercel or contacts providers."""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vercel.queue import RetryAfter

import delivery_queue
import queue_worker
import app as site
import sheets_sync
from backend import PhysicsPlusApp


class DeliveryQueueTests(unittest.TestCase):
    def test_local_runtime_keeps_synchronous_fallback(self) -> None:
        with patch.dict(os.environ, {"VERCEL": ""}):
            self.assertFalse(delivery_queue.publish_delivery_signal())

    def test_vercel_signal_contains_no_personal_data(self) -> None:
        with patch.dict(os.environ, {"VERCEL": "1"}), \
             patch("vercel.queue.sync.QueueClient") as client:
            self.assertTrue(delivery_queue.publish_delivery_signal())
        client.assert_called_once_with(timeout=2)
        args, kwargs = client.return_value.send.call_args
        self.assertEqual(args, ("physics-plus-delivery", {"kind": "drain", "v": 1}))
        self.assertEqual(kwargs["retention"], 7 * 24 * 60 * 60)

    def test_queue_failure_uses_existing_fallback(self) -> None:
        with patch.dict(os.environ, {"VERCEL": "1"}), \
             patch("vercel.queue.sync.QueueClient", side_effect=RuntimeError("offline")):
            self.assertFalse(delivery_queue.publish_delivery_signal())

    def test_worker_retries_until_database_outboxes_are_drained(self) -> None:
        with patch.object(queue_worker, "_drain_committed_outboxes", return_value=31):
            with self.assertRaises(RetryAfter):
                asyncio.run(queue_worker.drain_delivery_signal({"kind": "drain", "v": 1}))
        with patch.object(queue_worker, "_drain_committed_outboxes", return_value=None) as drain:
            asyncio.run(queue_worker.drain_delivery_signal({"kind": "drain", "v": 1}))
        drain.assert_called_once_with()

    def test_worker_ignores_unexpected_payload(self) -> None:
        with patch.object(queue_worker, "_drain_committed_outboxes") as drain:
            asyncio.run(queue_worker.drain_delivery_signal({"kind": "unknown", "v": 1}))
        drain.assert_not_called()

    def test_worker_drains_committed_sheet_revisions(self) -> None:
        class Recorder:
            def __init__(self):
                self.snapshots = []

            def write(self, snapshots):
                self.snapshots.append(snapshots)

        with tempfile.TemporaryDirectory() as directory:
            owner = PhysicsPlusApp(Path(directory) / "school.sqlite3", "https://school.example")
            recorder = Recorder()
            with patch.object(site, "_backend", return_value=owner), \
                 patch.object(site, "sheet_configured", return_value=True), \
                 patch.object(sheets_sync, "sheet_configured", return_value=True), \
                 patch.object(site, "dispatch_sheet_sync",
                              side_effect=lambda _owner, limit: sheets_sync.dispatch(
                                  _owner, limit=limit, client=recorder)):
                self.assertIsNone(queue_worker._drain_committed_outboxes())
            self.assertEqual(len(recorder.snapshots), 1)
            self.assertIn("courses", recorder.snapshots[0])
            with owner.connect() as conn:
                pending = conn.execute(
                    "SELECT COUNT(*) FROM sheet_sync_state WHERE revision>synced_revision"
                ).fetchone()[0]
            self.assertEqual(pending, 0)


if __name__ == "__main__":
    unittest.main()
