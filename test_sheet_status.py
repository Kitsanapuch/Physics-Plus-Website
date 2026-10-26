"""The admin sync indicator reflects committed, outstanding Sheet revisions."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import PhysicsPlusApp, SHEET_DATASETS, now


class SheetStatusTests(unittest.TestCase):
    def test_status_reports_pending_age_and_clears_after_sync(self):
        with tempfile.TemporaryDirectory() as temp:
            owner = PhysicsPlusApp(Path(temp) / "school.sqlite3", "https://school.example")
            with patch("sheets_sync.sheet_configured", return_value=True), patch(
                "sheets_sync.sheet_url", return_value="https://docs.google.com/spreadsheets/d/test/edit"
            ):
                with owner.connect() as conn:
                    first = owner._sheet_sync_status(conn)
                    self.assertEqual(first["pending"], len(SHEET_DATASETS))
                    self.assertIsNotNone(first["oldest_pending_at"])
                    self.assertGreaterEqual(first["pending_age_seconds"], 0)

                    conn.execute(
                        "UPDATE sheet_sync_state SET synced_revision=revision,synced_at=?,last_error_code=NULL",
                        (now(),),
                    )
                    caught_up = owner._sheet_sync_status(conn)
                    self.assertEqual(caught_up["pending"], 0)
                    self.assertIsNone(caught_up["oldest_pending_at"])
                    self.assertEqual(caught_up["pending_age_seconds"], 0)

                    conn.execute(
                        "UPDATE sheet_sync_state SET revision=revision+1,updated_at=? WHERE dataset=?",
                        (now() - 180, SHEET_DATASETS[0]),
                    )
                    late = owner._sheet_sync_status(conn)
                    self.assertEqual(late["pending"], 1)
                    self.assertGreaterEqual(late["pending_age_seconds"], 180)


if __name__ == "__main__":
    unittest.main()
