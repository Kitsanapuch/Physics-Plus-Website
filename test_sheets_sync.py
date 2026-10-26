"""Offline Google Sheets mirror checks. No test contacts Google or Turso."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app as site
from backend import BANGKOK, DEFAULT_PAYMENT_ACCOUNT, PORTAL_FEATURE_SCHEMA, PhysicsPlusApp, SCHEMA, SHEET_DATASETS, SHEET_SYNC_SCHEMA, now, uid
from remote import RemotePhysicsPlusApp
import remote
from sheets_sync import GoogleSheetsClient, SheetSyncError, _monthly_rows, dispatch


class RecordingSheets:
    def __init__(self, error: str | None = None):
        self.error = error
        self.calls = []

    def write(self, snapshots):
        self.calls.append(snapshots)
        if self.error:
            raise SheetSyncError(self.error)


class SheetSyncTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.owner = PhysicsPlusApp(Path(temp.name) / "school.sqlite3", "https://school.example")
        self.headers = {"host": "school.example", "origin": "https://school.example"}

    def _student(self):
        result = self.owner.handle("POST", "/api/auth/register", {
            "name": "Example Student", "email": "student@example.com",
            "password": "SyntheticPassword123!", "grade": "M4", "school": "School",
            "guardian_name": "Guardian", "guardian_phone": "0812345678",
            "guardian_relationship": "mother",
        }, self.headers, "127.0.0.1")
        self.assertEqual(result.status, 201, result.body)
        return result

    def test_initial_backfill_and_committed_write_are_mirrored(self):
        recorder = RecordingSheets()
        first = dispatch(self.owner, client=recorder)
        self.assertEqual(first, {"processed": len(SHEET_DATASETS), "synced": len(SHEET_DATASETS), "retrying": 0})
        self.assertEqual(len(recorder.calls[0]["courses"]), 16)
        self.assertEqual(len(recorder.calls[0]["payroll_monthly"]), 3)
        self.assertEqual(tuple(recorder.calls[0]["payment_account"][0][key]
                               for key in ("bank_code", "bank_name", "account_name", "account_number")),
                         DEFAULT_PAYMENT_ACCOUNT)

        self._student()
        with self.owner.connect() as conn:
            dirty = conn.execute(
                "SELECT COUNT(*) FROM sheet_sync_state WHERE revision>synced_revision"
            ).fetchone()[0]
        self.assertGreater(dirty, 0)
        second = dispatch(self.owner, client=recorder)
        self.assertEqual(second["synced"], dirty)
        self.assertEqual(recorder.calls[1]["students"][0]["guardian_name"], "Guardian")
        self.assertEqual(dispatch(self.owner, client=recorder)["processed"], 0)

    def test_teacher_rename_refreshes_all_reports_with_teacher_names(self):
        self.owner.bootstrap_admin(None, "Owner", "SafeTestPassword123!", "physicsplus")
        with self.owner.connect() as conn:
            teacher_id = conn.execute(
                "SELECT id FROM users WHERE role='teacher' LIMIT 1"
            ).fetchone()[0]
            admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
            course_id = conn.execute(
                "SELECT c.id FROM courses c LEFT JOIN course_formats f ON f.course_id=c.id "
                "WHERE f.course_id IS NULL LIMIT 1"
            ).fetchone()[0]
            conn.execute("UPDATE courses SET teacher_id=? WHERE id=?", (teacher_id, course_id))
            slot_id, task_id, current = uid(), uid(), now()
            conn.execute(
                "INSERT INTO teacher_availability(id,teacher_id,starts_at,ends_at,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (slot_id, teacher_id, current + 3600, current + 7200, admin_id, current),
            )
            conn.execute(
                "INSERT INTO assignments(id,assignee_id,course_id,title,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (task_id, teacher_id, course_id, "Synthetic assignment", admin_id, current, current),
            )
        dispatch(self.owner, client=RecordingSheets())
        login = self.owner.handle("POST", "/api/auth/login", {
            "login": "physicsplus", "password": "SafeTestPassword123!",
        }, self.headers, "127.0.0.1")
        admin_headers = {
            **self.headers,
            "cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "x-csrf-token": login.body["csrf_token"],
        }
        changed = self.owner.handle("PUT", f"/api/admin/users/{teacher_id}", {
            "name": "Renamed Teacher",
        }, admin_headers, "127.0.0.1")
        self.assertEqual(changed.status, 200, changed.body)
        with self.owner.connect() as conn:
            queued = conn.execute(
                "SELECT dataset,revision,synced_revision FROM sheet_sync_state "
                "WHERE dataset IN ('courses','teacher_availability','assignments')"
            ).fetchall()
            self.assertEqual(len(queued), 3)
            self.assertTrue(all(row["revision"] > row["synced_revision"] for row in queued))
        recorder = RecordingSheets()
        dispatch(self.owner, client=recorder)
        changed_course = next(row for row in recorder.calls[0]["courses"] if row["id"] == course_id)
        self.assertEqual(changed_course["teacher_name"], "Renamed Teacher")
        changed_slot = next(row for row in recorder.calls[0]["teacher_availability"] if row["id"] == slot_id)
        changed_task = next(row for row in recorder.calls[0]["assignments"] if row["id"] == task_id)
        self.assertEqual(changed_slot["teacher_name"], "Renamed Teacher")
        self.assertEqual(changed_task["assignee_name"], "Renamed Teacher")

    def test_course_update_queues_course_linked_assignment_report(self):
        self.owner.bootstrap_admin(None, "Owner", "SafeTestPassword123!", "physicsplus")
        dispatch(self.owner, client=RecordingSheets())
        login = self.owner.handle("POST", "/api/auth/login", {
            "login": "physicsplus", "password": "SafeTestPassword123!",
        }, self.headers, "127.0.0.2")
        admin_headers = {
            **self.headers,
            "cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "x-csrf-token": login.body["csrf_token"],
        }
        with self.owner.connect() as conn:
            course_id = conn.execute(
                "SELECT c.id FROM courses c LEFT JOIN course_formats f ON f.course_id=c.id "
                "WHERE f.course_id IS NULL LIMIT 1"
            ).fetchone()[0]
        updated = self.owner.handle("PUT", f"/api/admin/courses/{course_id}", {
            "price_satang": 375_000,
        }, admin_headers, "127.0.0.2")
        self.assertEqual(updated.status, 200, updated.body)
        with self.owner.connect() as conn:
            row = conn.execute(
                "SELECT revision,synced_revision FROM sheet_sync_state WHERE dataset='assignments'"
            ).fetchone()
        self.assertGreater(row["revision"], row["synced_revision"])

    def test_failed_delivery_stays_pending_and_can_retry(self):
        failed = dispatch(self.owner, client=RecordingSheets("provider_http_503"))
        self.assertEqual(failed["retrying"], len(SHEET_DATASETS))
        with self.owner.connect() as conn:
            row = conn.execute(
                "SELECT revision,synced_revision,last_error_code,next_attempt_at "
                "FROM sheet_sync_state WHERE dataset='students'"
            ).fetchone()
            self.assertGreater(row["revision"], row["synced_revision"])
            self.assertEqual(row["last_error_code"], "provider_http_503")
            self.assertGreater(row["next_attempt_at"], now())
            conn.execute("UPDATE sheet_sync_state SET next_attempt_at=0")
        recovered = dispatch(self.owner, client=RecordingSheets())
        self.assertEqual(recovered["synced"], len(SHEET_DATASETS))

    def test_revision_added_during_delivery_is_not_lost(self):
        class WriteDuringDelivery(RecordingSheets):
            def __init__(self, owner):
                super().__init__()
                self.owner = owner

            def write(self, snapshots):
                super().write(snapshots)
                with self.owner.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self.owner.audit(conn, None, "student_profile", "synthetic", "update")
                    conn.commit()

        result = dispatch(self.owner, client=WriteDuringDelivery(self.owner))
        self.assertEqual(result["synced"], len(SHEET_DATASETS))
        with self.owner.connect() as conn:
            row = conn.execute(
                "SELECT revision,synced_revision FROM sheet_sync_state WHERE dataset='students'"
            ).fetchone()
        self.assertEqual(row["revision"], row["synced_revision"] + 1)

    def test_expired_lease_writer_cannot_mark_newer_sheet_snapshot_clean(self):
        class LeaseTakenOver(RecordingSheets):
            def __init__(self, owner):
                super().__init__()
                self.owner = owner

            def write(self, snapshots):
                super().write(snapshots)
                with self.owner.connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute(
                        "UPDATE sheet_sync_state SET lease_token='newer-worker',"
                        "lease_until=0,synced_revision=revision"
                    )
                    conn.commit()

        result = dispatch(self.owner, client=LeaseTakenOver(self.owner))
        self.assertEqual(result["synced"], 0)
        with self.owner.connect() as conn:
            row = conn.execute(
                "SELECT revision,synced_revision FROM sheet_sync_state WHERE dataset='students'"
            ).fetchone()
        self.assertGreater(row["revision"], row["synced_revision"])

    def test_payment_account_is_private_and_admin_managed(self):
        self.assertEqual(self.owner.handle("GET", "/api/payment-account", None,
                                           self.headers, "127.0.0.1").status, 401)
        student = self._student()
        cookie = student.headers["Set-Cookie"].split(";", 1)[0]
        student_headers = {**self.headers, "cookie": cookie,
                           "x-csrf-token": student.body["csrf_token"]}
        account = self.owner.handle("GET", "/api/payment-account", None,
                                    student_headers, "127.0.0.1")
        self.assertEqual(account.status, 200)
        self.assertEqual(account.body["payment_account"]["account_number"], "585-2-22394-9")
        self.assertEqual(self.owner.handle("PUT", "/api/admin/payment-account", {},
                                           student_headers, "127.0.0.1").status, 403)

        self.owner.bootstrap_admin(None, "Owner", "SafeTestPassword123!", "physicsplus")
        login = self.owner.handle("POST", "/api/auth/login", {
            "login": "physicsplus", "password": "SafeTestPassword123!",
        }, self.headers, "127.0.0.2")
        self.assertEqual(login.status, 200)
        admin_headers = {**self.headers,
                         "cookie": login.headers["Set-Cookie"].split(";", 1)[0],
                         "x-csrf-token": login.body["csrf_token"]}
        changed = self.owner.handle("PUT", "/api/admin/payment-account", {
            "bank_code": "ttb", "bank_name": "ทีเอ็มบีธนชาต",
            "account_name": "ชดาภัษท สุดศิริ", "account_number": "585-2-22394-9",
        }, admin_headers, "127.0.0.2")
        self.assertEqual(changed.status, 200, changed.body)
        self.assertEqual(changed.body["payment_account"]["account_name"], "ชดาภัษท สุดศิริ")
        self.assertEqual(self.owner.handle("GET", "/api/admin/sheet-sync/status", None,
                                           student_headers, "127.0.0.1").status, 403)

    def test_monthly_summary_excludes_pending_and_paid_from_due(self):
        with self.owner.connect() as conn:
            teacher = conn.execute("SELECT id FROM users WHERE role='teacher' LIMIT 1").fetchone()[0]
            course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()[0]
            class_id = uid()
            start = now() - 3600
            conn.execute("INSERT INTO class_sessions(id,course_id,room_name,teacher_id,starts_at,ends_at,created_at) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (class_id, course, "A", teacher, start, start + 3600, now()))
            conn.execute("INSERT INTO time_entries(id,session_id,teacher_id,actual_start,actual_end,"
                         "status,payable_minutes,amount_satang,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (uid(), class_id, teacher, start, start + 3600, "approved", 60, 25_000, now()))
            rows = _monthly_rows(conn)
        item = next(row for row in rows if row["teacher_name"] and row["total_wages_due_satang"] == 25_000)
        self.assertEqual(item["approved_count"], 1)
        self.assertEqual(item["approved_minutes"], 60)
        self.assertTrue(item["is_current_month"])

    def test_new_month_starts_at_zero_without_erasing_prior_teacher_wages(self):
        previous_month = (datetime.now(BANGKOK).replace(day=1, hour=12, minute=0,
                                                        second=0, microsecond=0) - timedelta(days=1))
        with self.owner.connect() as conn:
            teacher = conn.execute("SELECT id FROM users WHERE role='teacher' LIMIT 1").fetchone()[0]
            teacher_code = conn.execute("SELECT public_code FROM users WHERE id=?", (teacher,)).fetchone()[0]
            course = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()[0]
            class_id = uid()
            start = int(previous_month.timestamp())
            conn.execute(
                "INSERT INTO class_sessions(id,course_id,room_name,teacher_id,starts_at,ends_at,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (class_id, course, "A", teacher, start, start + 3600, now()),
            )
            conn.execute(
                "INSERT INTO time_entries(id,session_id,teacher_id,actual_start,actual_end,"
                "status,payable_minutes,amount_satang,submitted_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid(), class_id, teacher, start, start + 3600, "approved", 60, 25_000, now()),
            )
            rows = [row for row in _monthly_rows(conn) if row["teacher_code"] == teacher_code]
        self.assertEqual(len(rows), 2)
        current_row = next(row for row in rows if row["is_current_month"])
        past_row = next(row for row in rows if not row["is_current_month"])
        self.assertEqual(current_row["total_wages_due_satang"], 0)
        self.assertEqual(past_row["total_wages_due_satang"], 25_000)
        self.assertEqual(past_row["period"], previous_month.strftime("%Y-%m"))

    def test_flask_save_and_admin_retry_attempt_delivery_after_commit(self):
        recorder = RecordingSheets()
        self.owner.bootstrap_admin(None, "Owner", "SafeTestPassword123!", "physicsplus")
        with patch.object(site, "_backend", return_value=self.owner), \
             patch.object(site, "publish_delivery_signal", return_value=False), \
             patch.object(site, "sheet_configured", return_value=True), \
             patch.object(site, "dispatch_sheet_sync",
                          side_effect=lambda _owner, limit: dispatch(_owner, limit=limit, client=recorder)):
            student = site.app.test_client()
            response = student.post("/api/auth/register", base_url="https://school.example",
                                    headers={"Origin": "https://school.example"}, json={
                "name": "Example Student", "email": "student@example.com",
                "password": "SyntheticPassword123!", "grade": "M4", "school": "School",
                "guardian_name": "Guardian", "guardian_phone": "0812345678",
                "guardian_relationship": "mother",
            })
            self.assertEqual(response.status_code, 201, response.json)
            self.assertEqual(len(recorder.calls), 1)
            self.assertEqual(recorder.calls[0]["students"][0]["name"], "Example Student")

            admin = site.app.test_client()
            login = admin.post("/api/auth/login", base_url="https://school.example",
                               headers={"Origin": "https://school.example"},
                               json={"login": "physicsplus", "password": "SafeTestPassword123!"})
            headers = {"Origin": "https://school.example",
                       "X-CSRF-Token": login.json["csrf_token"]}
            retried = admin.post("/api/admin/sheet-sync/retry", base_url="https://school.example",
                                 headers=headers, json={})
            self.assertEqual(retried.status_code, 200, retried.json)
            self.assertEqual(retried.json["synced"], len(SHEET_DATASETS))
            self.assertEqual(retried.json["pending"], 0)

    def test_accepted_delivery_signal_returns_without_waiting_for_providers(self):
        with patch.object(site, "_backend", return_value=self.owner), \
             patch.object(site, "publish_delivery_signal", return_value=True) as publisher, \
             patch.object(site, "_dispatch_sheets", side_effect=AssertionError("provider called")), \
             patch.object(site, "_dispatch_notifications", side_effect=AssertionError("mail called")):
            response = site.app.test_client().post(
                "/api/auth/register", base_url="https://school.example",
                headers={"Origin": "https://school.example"}, json={
                    "name": "Queued Student", "email": "queued@example.com",
                    "password": "SyntheticPassword123!", "grade": "M4", "school": "School",
                    "guardian_name": "Guardian", "guardian_phone": "0812345678",
                    "guardian_relationship": "mother",
                },
            )
        self.assertEqual(response.status_code, 201, response.json)
        publisher.assert_called_once_with()
        with self.owner.connect() as conn:
            self.assertGreater(conn.execute(
                "SELECT COUNT(*) FROM sheet_sync_state WHERE revision>synced_revision"
            ).fetchone()[0], 0)
            self.assertGreater(conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE status='pending'"
            ).fetchone()[0], 0)


class FakeResponse:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class FakeHttp:
    def __init__(self):
        self.calls = []

    def request(self, method, url, json=None, timeout=None):
        self.calls.append((method, url, json, timeout))
        if method == "GET" and len(self.calls) == 1:
            return FakeResponse({"sheets": [{"properties": {
                "sheetId": 1, "title": "สรุปยอด", "gridProperties": {"rowCount": 1000, "columnCount": 26}
            }}]})
        if method == "POST" and len(self.calls) == 2:
            return FakeResponse({"replies": [{"addSheet": {"properties": {"sheetId": 2}}}]})
        if method == "GET" and len(self.calls) == 3:
            return FakeResponse({"sheets": [
                {"properties": {"sheetId": 1, "title": "สรุปยอด", "gridProperties": {"rowCount": 1000, "columnCount": 26}}},
                {"properties": {"sheetId": 2, "title": "DB_students", "gridProperties": {"rowCount": 1000, "columnCount": 26}}},
            ]})
        return FakeResponse({"replies": []})


class GoogleWriterTests(unittest.TestCase):
    def test_only_dedicated_tab_is_changed_and_formula_like_text_is_literal(self):
        writer = GoogleSheetsClient.__new__(GoogleSheetsClient)
        writer.http = FakeHttp()
        writer.base = "https://sheets.googleapis.com/v4/spreadsheets/synthetic-id"
        writer.write({"students": [{"id": "=IMPORTRANGE(\"x\",\"y\")", "name": "A"}]})
        self.assertEqual(len(writer.http.calls), 4)
        batch = writer.http.calls[-1][2]["requests"]
        self.assertTrue(all(request[list(request)[0]].get("range", {}).get("sheetId", 2) == 2
                            for request in batch if "repeatCell" in request))
        cleared = next(request["repeatCell"] for request in batch
                       if "repeatCell" in request and request["repeatCell"]["fields"] == "userEnteredValue")
        # A previous report schema may have populated columns beyond today's
        # headers; stale personal details must be removed from the mirror.
        self.assertEqual(cleared["range"]["endColumnIndex"], 26)
        data = next(request["updateCells"] for request in batch if "updateCells" in request)
        self.assertEqual(data["start"]["sheetId"], 2)
        self.assertEqual(data["rows"][0]["values"][0]["userEnteredValue"]["stringValue"],
                         "student_code")
        self.assertEqual(data["rows"][0]["values"][-1]["userEnteredValue"]["stringValue"],
                         "internal_id")
        self.assertEqual(data["rows"][1]["values"][-1]["userEnteredValue"]["stringValue"],
                         '=IMPORTRANGE("x","y")')


class RemoteAdditiveMigrationTests(unittest.TestCase):
    def test_initializer_requeues_previously_clean_sheet_once(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "synced.sqlite3"
            PhysicsPlusApp(database, "https://school.example")
            with sqlite3.connect(database) as conn:
                conn.execute("UPDATE sheet_sync_state SET synced_revision=revision")

            def local_transport(_url, *, auth_token=None, isolation_level=None):
                return sqlite3.connect(database, isolation_level=isolation_level)

            with patch.object(remote.turso_serverless, "connect", local_transport):
                owner = RemotePhysicsPlusApp("turso://test.example", "synthetic-token",
                                             "https://school.example")
                owner.initialize_remote()
                with owner.connect() as conn:
                    first = dict(conn.execute(
                        "SELECT dataset,revision FROM sheet_sync_state"
                    ).fetchall())
                    self.assertEqual(conn.execute(
                        "SELECT COUNT(*) FROM sheet_sync_state "
                        "WHERE revision>synced_revision"
                    ).fetchone()[0], len(SHEET_DATASETS))
                owner.initialize_remote()
                with owner.connect() as conn:
                    second = dict(conn.execute(
                        "SELECT dataset,revision FROM sheet_sync_state"
                    ).fetchall())
                self.assertEqual(first, second)

    def test_existing_database_gets_new_tables_before_first_write(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "old.sqlite3"
            with sqlite3.connect(database) as conn:
                conn.executescript(SCHEMA.replace(SHEET_SYNC_SCHEMA, ""))

            def local_transport(_url, *, auth_token=None, isolation_level=None):
                return sqlite3.connect(database, isolation_level=isolation_level)

            with patch.object(remote.turso_serverless, "connect", local_transport):
                owner = RemotePhysicsPlusApp("turso://test.example", "synthetic-token",
                                             "https://school.example")
                with owner.connect() as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM sheet_sync_state").fetchone()[0],
                                     len(SHEET_DATASETS))
                    self.assertEqual(conn.execute("SELECT account_number FROM payment_account WHERE id=1")
                                     .fetchone()[0], "585-2-22394-9")

    def test_existing_remote_database_gets_portal_tables_on_first_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            database = Path(folder) / "pre-portal.sqlite3"
            with sqlite3.connect(database) as conn:
                conn.executescript(SCHEMA.replace(PORTAL_FEATURE_SCHEMA, ""))

            def local_transport(_url, *, auth_token=None, isolation_level=None):
                return sqlite3.connect(database, isolation_level=isolation_level)

            with patch.object(remote.turso_serverless, "connect", local_transport):
                owner = RemotePhysicsPlusApp("turso://test.example", "synthetic-token",
                                             "https://school.example")
                with owner.connect() as conn:
                    tables = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )}
                    self.assertTrue({"course_formats", "course_progress_targets",
                                     "teacher_availability", "inbox_notifications", "assignments"}
                                    .issubset(tables))
                    self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
