"""Transactional outbox checks; no test sends a real email."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import app as site
from backend import PhysicsPlusApp, migrate_outbox_schema, now, uid
from mailer import MailResult


class NotificationOutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        mode = patch.dict(os.environ, {"PH_EMAIL_MODE": "email"})
        mode.start()
        self.addCleanup(mode.stop)
        self.owner = PhysicsPlusApp(Path(temp.name) / "test.sqlite3", "https://school.example")
        self.origin_headers = {"host": "school.example", "origin": "https://school.example"}

    def register_student(self) -> str:
        result = self.owner.handle(
            "POST", "/api/auth/register",
            {"name": "Student", "email": "student@example.com", "password": "SyntheticPassword123!",
             "grade": "M4", "school": "School", "guardian_name": "Guardian",
             "guardian_phone": "0812345678", "guardian_relationship": "mother"},
            self.origin_headers, "127.0.0.1",
        )
        self.assertEqual(result.status, 201, result.body)
        self.student_cookie = result.headers["Set-Cookie"].split(";", 1)[0]
        return result.body["user"]["id"]

    def test_registration_queues_admin_notice_even_without_email_configuration(self) -> None:
        self.register_student()
        with self.owner.connect() as conn:
            row = conn.execute(
                "SELECT n.event_type,n.recipient_kind,n.recipient_user_id,n.status,"
                "a.entity,a.action FROM notification_outbox n "
                "JOIN audit_events a ON a.id=n.audit_event_id"
            ).fetchone()
        self.assertEqual(tuple(row), ("student_registered", "admin", None, "pending", "user", "register"))

    def test_inbox_only_preserves_existing_email_rows_and_personal_notifications(self) -> None:
        student_id = self.register_student()
        self.owner.bootstrap_admin(None, "Admin", "SyntheticPassword123!", "physicsplus")
        login = self.owner.handle(
            "POST", "/api/auth/login",
            {"login": "physicsplus", "password": "SyntheticPassword123!"},
            self.origin_headers, "127.0.0.2",
        )
        self.assertEqual(login.status, 200, login.body)
        admin_headers = {
            **self.origin_headers,
            "cookie": login.headers["Set-Cookie"].split(";", 1)[0],
            "x-csrf-token": login.body["csrf_token"],
        }
        with self.owner.connect() as conn:
            old_row = conn.execute(
                "SELECT id,status,attempts,next_attempt_at FROM notification_outbox"
            ).fetchone()
            old_state = tuple(old_row)

        with patch.dict(os.environ, {
            "PH_EMAIL_MODE": "inbox_only",
            "PH_GMAIL_USER": "physicsplus.test@gmail.com",
            "PH_GMAIL_APP_PASSWORD": "abcdefghijklmnop",
        }):
            with self.owner.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                event_id = self.owner.audit(conn, student_id, "invoice", uid(), "create")
                self.owner.enqueue_notice(conn, event_id, "invoice_created", student_id)
                conn.commit()
                self.assertIsNone(conn.execute(
                    "SELECT id FROM notification_outbox WHERE audit_event_id=?", (event_id,)
                ).fetchone())
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM inbox_notifications WHERE audit_event_id=? AND recipient_user_id=?",
                    (event_id, student_id),
                ).fetchone()[0], 1)

            inbox = self.owner.handle(
                "GET", "/api/notifications", None,
                {"host": "school.example", "cookie": self.student_cookie}, "127.0.0.1",
            )
            self.assertEqual(inbox.status, 200, inbox.body)
            self.assertTrue(any(row["event_type"] == "invoice_created"
                                for row in inbox.body["notifications"]))
            status = self.owner.handle(
                "GET", "/api/admin/notification-status", None,
                admin_headers, "127.0.0.2",
            )
            self.assertEqual(status.status, 200, status.body)
            self.assertEqual(status.body["mode"], "inbox_only")
            self.assertEqual(status.body["counts"]["pending"], 1)
            compact = self.owner.handle(
                "GET", "/api/admin/dashboard?compact=1", None,
                admin_headers, "127.0.0.2",
            )
            self.assertEqual(compact.status, 200, compact.body)
            self.assertEqual(compact.body["notifications"]["mode"], "inbox_only")
            for endpoint in ("notification-test", "notification-retry"):
                denied = self.owner.handle(
                    "POST", f"/api/admin/{endpoint}", {}, admin_headers, "127.0.0.2",
                )
                self.assertEqual((denied.status, denied.body["error"]["code"]),
                                 (503, "email_disabled"))
            sent_to = []
            outcome = self.owner.dispatch_outbox(
                lambda address, *, event_id: sent_to.append(address) or MailResult(True, "accepted"),
                limit=2, admin_email="admin@example.com",
            )
            self.assertEqual(outcome, {"processed": 0, "sent": 0, "retrying": 0, "dead": 0})
            self.assertEqual(sent_to, [])

        with self.owner.connect() as conn:
            preserved = conn.execute(
                "SELECT id,status,attempts,next_attempt_at FROM notification_outbox WHERE id=?",
                (old_row["id"],),
            ).fetchone()
            self.assertEqual(tuple(preserved), old_state)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity IN ('email_test','email_delivery')"
            ).fetchone()[0], 0)

        resumed_status = self.owner.handle(
            "GET", "/api/admin/notification-status", None, admin_headers, "127.0.0.2",
        )
        self.assertEqual(resumed_status.body["mode"], "email")

    def test_registration_rejects_addresses_mailing_provider_cannot_send(self) -> None:
        for bad_email in ("student@exam..ple.com", "student@-example.com", "student@exámple.com", "student..name@example.com"):
            with self.subTest(email=bad_email):
                result = self.owner.handle(
                    "POST", "/api/auth/register",
                    {"name": "Student", "email": bad_email, "password": "SyntheticPassword123!",
                     "grade": "M4", "school": "School", "guardian_name": "Guardian",
                     "guardian_phone": "0812345678", "guardian_relationship": "mother"},
                    self.origin_headers, "127.0.0.1",
                )
                self.assertEqual(result.status, 400)
                self.assertEqual(result.body["error"]["code"], "invalid_email")
        with self.owner.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0], 0)

    def test_duplicate_enqueue_is_ignored_and_sent_once(self) -> None:
        student_id = self.register_student()
        with self.owner.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event_id = self.owner.audit(conn, student_id, "invoice", uid(), "create")
            self.owner.enqueue_notice(conn, event_id, "invoice_created", student_id)
            self.owner.enqueue_notice(conn, event_id, "invoice_created", student_id)
            conn.commit()
        received: list[tuple[str, str]] = []

        def fake_send(address: str, *, event_id: str) -> MailResult:
            received.append((address, event_id))
            return MailResult(True, "accepted", provider_id="synthetic")

        self.assertEqual(self.owner.dispatch_outbox(fake_send, limit=2)["sent"], 1)
        self.assertEqual(self.owner.dispatch_outbox(fake_send, limit=2)["sent"], 0)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0][0], "student@example.com")
        with self.owner.connect() as conn:
            rows = conn.execute("SELECT id,status,attempts,sent_at FROM notification_outbox WHERE audit_event_id=?", (event_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "sent")
        self.assertEqual(rows[0]["attempts"], 1)
        self.assertIsNotNone(rows[0]["sent_at"])

    def test_admin_destination_resolved_at_dispatch_and_retry_keeps_id(self) -> None:
        self.register_student()
        sent_to: list[tuple[str, str]] = []

        def temporary_failure(address: str, *, event_id: str) -> MailResult:
            sent_to.append((address, event_id))
            return MailResult(False, "temporary_failure", retryable=True)

        self.assertEqual(self.owner.dispatch_outbox(temporary_failure, limit=1, admin_email="")["processed"], 0)
        first = self.owner.dispatch_outbox(temporary_failure, limit=1, admin_email="admin@example.com")
        self.assertEqual(first["retrying"], 1)
        with self.owner.connect() as conn:
            row = conn.execute("SELECT id,status,attempts,next_attempt_at FROM notification_outbox").fetchone()
            self.assertEqual(row["status"], "pending")
            self.assertGreater(row["next_attempt_at"], now())
            conn.execute("UPDATE notification_outbox SET next_attempt_at=? WHERE id=?", (now(), row["id"]))
        def accepted(address: str, *, event_id: str) -> MailResult:
            sent_to.append((address, event_id))
            return MailResult(True, "accepted", provider_id="synthetic")
        self.assertEqual(self.owner.dispatch_outbox(accepted, limit=1, admin_email="admin@example.com")["sent"], 1)
        self.assertEqual(sent_to, [("admin@example.com", row["id"]), ("admin@example.com", row["id"])])

    def test_concurrent_workers_claim_only_one_notice(self) -> None:
        student_id = self.register_student()
        with self.owner.connect() as conn:
            conn.execute("DELETE FROM notification_outbox")
            conn.execute("BEGIN IMMEDIATE")
            event_id = self.owner.audit(conn, student_id, "invoice", uid(), "create")
            self.owner.enqueue_notice(conn, event_id, "invoice_created", student_id)
            conn.commit()
        barrier = threading.Barrier(2)
        calls: list[str] = []
        lock = threading.Lock()

        def fake_send(address: str, *, event_id: str) -> MailResult:
            with lock:
                calls.append(event_id)
            time.sleep(0.05)
            return MailResult(True, "accepted", provider_id="synthetic")

        def worker() -> dict[str, int]:
            barrier.wait()
            return self.owner.dispatch_outbox(fake_send, limit=1)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sum(result["sent"] for result in results), 1)
        self.assertEqual(len(calls), 1)

    def test_transient_failure_retries_on_next_day_hobby_cron(self) -> None:
        self.register_student()
        called: list[str] = []

        def uncertain(address: str, *, event_id: str) -> MailResult:
            called.append(event_id)
            return MailResult(False, "temporary_failure", retryable=True, status=503)

        self.assertEqual(self.owner.dispatch_outbox(uncertain, limit=1, admin_email="admin@example.com")["retrying"], 1)
        with self.owner.connect() as conn:
            conn.execute(
                "UPDATE notification_outbox SET first_attempt_at=?,next_attempt_at=?",
                (now() - 24 * 3600 - 60, now()),
            )
        result = self.owner.dispatch_outbox(
            lambda address, *, event_id: (called.append(event_id) or MailResult(True, "accepted", provider_id="synthetic")),
            limit=1, admin_email="admin@example.com",
        )
        self.assertEqual(result["sent"], 1)
        self.assertEqual(len(called), 2)
        self.assertEqual(called[0], called[1])
        with self.owner.connect() as conn:
            row = conn.execute("SELECT status,last_error_code FROM notification_outbox").fetchone()
        self.assertEqual(tuple(row), ("sent", None))

    def test_expired_retry_is_dead_without_another_send(self) -> None:
        self.register_student()
        calls: list[str] = []
        self.owner.dispatch_outbox(
            lambda address, *, event_id: MailResult(False, "temporary_failure", retryable=True, status=503),
            limit=1, admin_email="admin@example.com",
        )
        with self.owner.connect() as conn:
            conn.execute("UPDATE notification_outbox SET first_attempt_at=?,next_attempt_at=?",
                         (now() - 8 * 24 * 3600, now()))
        result = self.owner.dispatch_outbox(
            lambda address, *, event_id: (calls.append(event_id) or MailResult(True, "accepted")),
            limit=1, admin_email="admin@example.com",
        )
        self.assertEqual(result["processed"], 0)
        self.assertEqual(calls, [])
        with self.owner.connect() as conn:
            row = conn.execute("SELECT status,last_error_code FROM notification_outbox").fetchone()
        self.assertEqual(tuple(row), ("dead", "retry_expired"))

    def test_rate_limit_can_retry_on_next_day_cron(self) -> None:
        self.register_student()
        first = self.owner.dispatch_outbox(
            lambda address, *, event_id: MailResult(False, "temporary_failure", retryable=True, status=429),
            limit=1, admin_email="admin@example.com",
        )
        self.assertEqual(first["retrying"], 1)
        with self.owner.connect() as conn:
            conn.execute(
                "UPDATE notification_outbox SET first_attempt_at=?,next_attempt_at=?",
                (now() - 24 * 3600, now()),
            )
        second = self.owner.dispatch_outbox(
            lambda address, *, event_id: MailResult(True, "accepted", provider_id="synthetic"),
            limit=1, admin_email="admin@example.com",
        )
        self.assertEqual(second["sent"], 1)

    def test_distinct_gmail_configuration_failures_remain_retryable(self) -> None:
        self.register_student()
        for code in ("invalid_gmail_sender", "invalid_app_password_format",
                     "gmail_auth_rejected", "smtp_tls_failure"):
            with self.subTest(code=code):
                with self.owner.connect() as conn:
                    conn.execute(
                        "UPDATE notification_outbox SET status='pending',attempts=0,"
                        "first_attempt_at=?,next_attempt_at=?,last_error_code=?",
                        (now() - 8 * 24 * 3600, now(), code),
                    )
                batch = self.owner.dispatch_outbox(
                    lambda address, *, event_id: MailResult(False, code),
                    limit=1, admin_email="admin@example.com",
                )
                self.assertEqual(batch["retrying"], 1)
                with self.owner.connect() as conn:
                    row = conn.execute(
                        "SELECT status,attempts,first_attempt_at,last_error_code,next_attempt_at "
                        "FROM notification_outbox"
                    ).fetchone()
                self.assertEqual((row["status"], row["attempts"], row["first_attempt_at"],
                                  row["last_error_code"]), ("pending", 0, None, code))
                self.assertGreater(row["next_attempt_at"], now())

    def test_notification_status_is_admin_only_and_has_no_address(self) -> None:
        student_id = self.register_student()
        student_view = self.owner.handle(
            "GET", "/api/admin/notification-status", None,
            {"host": "school.example", "cookie": self.student_cookie}, "127.0.0.1",
        )
        self.assertEqual(student_view.status, 403)
        with self.owner.connect() as conn:
            conn.execute("UPDATE notification_outbox SET status='dead',last_error_code='provider_rejected'")
        self.owner.bootstrap_admin(None, "Admin", "SyntheticPassword123!", "physicsplus")
        login = self.owner.handle(
            "POST", "/api/auth/login", {"login": "physicsplus", "password": "SyntheticPassword123!"},
            self.origin_headers, "127.0.0.2",
        )
        self.assertEqual(login.status, 200, login.body)
        admin_view = self.owner.handle(
            "GET", "/api/admin/notification-status", None,
            {"host": "school.example", "cookie": login.headers["Set-Cookie"].split(";", 1)[0]},
            "127.0.0.2",
        )
        self.assertEqual(admin_view.status, 200)
        self.assertEqual(admin_view.body["counts"]["dead"], 1)
        self.assertEqual(admin_view.body["failed"][0]["entity"], "user")
        self.assertEqual(admin_view.body["failed"][0]["entity_id"], student_id)
        self.assertIsNone(admin_view.body["failed"][0]["recipient_user_id"])
        self.assertEqual(admin_view.body["recent"][0]["status"], "dead")
        self.assertEqual(admin_view.body["recent"][0]["last_error_code"], "provider_rejected")
        self.assertEqual(
            set(admin_view.body["recent"][0]),
            {"event_type", "status", "attempts", "last_error_code", "created_at",
             "next_attempt_at", "sent_at"},
        )
        self.assertNotIn("student@example.com", str(admin_view.body))

    def test_admin_retry_reschedules_only_configuration_failures(self) -> None:
        self.register_student()
        self.owner.bootstrap_admin(None, "Admin", "SyntheticPassword123!", "physicsplus")
        login = self.owner.handle(
            "POST", "/api/auth/login",
            {"login": "physicsplus", "password": "SyntheticPassword123!"},
            self.origin_headers, "127.0.0.2",
        )
        self.assertEqual(login.status, 200, login.body)
        admin_cookie = login.headers["Set-Cookie"].split(";", 1)[0]
        admin_headers = {**self.origin_headers, "cookie": admin_cookie,
                         "x-csrf-token": login.body["csrf_token"]}
        student_me = self.owner.handle(
            "GET", "/api/me", None,
            {"host": "school.example", "cookie": self.student_cookie}, "127.0.0.1",
        )
        student_headers = {**self.origin_headers, "cookie": self.student_cookie,
                           "x-csrf-token": student_me.body["csrf_token"]}

        with self.owner.connect() as conn:
            first = conn.execute("SELECT id FROM notification_outbox").fetchone()["id"]
            conn.execute(
                "UPDATE notification_outbox SET last_error_code='gmail_auth_rejected',"
                "next_attempt_at=? WHERE id=?", (now() + 3600, first),
            )
            conn.execute("BEGIN IMMEDIATE")
            event_id = self.owner.audit(conn, None, "invoice", uid(), "create")
            self.owner.enqueue_notice(conn, event_id, "invoice_created", None)
            conn.commit()
            other = conn.execute(
                "SELECT id FROM notification_outbox WHERE audit_event_id=?", (event_id,)
            ).fetchone()["id"]
            conn.execute(
                "UPDATE notification_outbox SET last_error_code='temporary_failure',"
                "next_attempt_at=? WHERE id=?", (now() + 3600, other),
            )

        self.assertEqual(self.owner.handle(
            "POST", "/api/admin/notification-retry", {}, student_headers, "127.0.0.1",
        ).status, 403)
        self.assertEqual(self.owner.handle(
            "POST", "/api/admin/notification-retry", {},
            {**self.origin_headers, "cookie": admin_cookie}, "127.0.0.2",
        ).status, 403)
        with patch.dict(os.environ, {}, clear=True):
            missing = self.owner.handle(
                "POST", "/api/admin/notification-retry", {}, admin_headers, "127.0.0.2",
            )
        self.assertEqual((missing.status, missing.body["error"]["code"]),
                         (503, "email_unconfigured"))

        config = {"PH_GMAIL_USER": "school@gmail.com",
                  "PH_GMAIL_APP_PASSWORD": "abcdefghijklmnop"}
        with patch.dict(os.environ, config, clear=True):
            invalid = self.owner.handle(
                "POST", "/api/admin/notification-retry", {"recipient": "other@example.com"},
                admin_headers, "127.0.0.2",
            )
            queued = self.owner.handle(
                "POST", "/api/admin/notification-retry", {}, admin_headers, "127.0.0.2",
            )
            repeated = self.owner.handle(
                "POST", "/api/admin/notification-retry", {}, admin_headers, "127.0.0.2",
            )
        self.assertEqual((invalid.status, invalid.body["error"]["code"]),
                         (400, "invalid_input"))
        self.assertEqual((queued.status, queued.body), (200, {"queued": 1}))
        self.assertEqual((repeated.status, repeated.body["error"]["code"]),
                         (429, "rate_limited"))
        with self.owner.connect() as conn:
            first_row = conn.execute(
                "SELECT status,next_attempt_at,last_error_code FROM notification_outbox WHERE id=?",
                (first,),
            ).fetchone()
            other_row = conn.execute(
                "SELECT next_attempt_at,last_error_code FROM notification_outbox WHERE id=?",
                (other,),
            ).fetchone()
            retry_audits = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE entity='email_delivery' AND action='retry'"
            ).fetchone()[0]
        self.assertEqual(first_row["status"], "pending")
        self.assertLessEqual(first_row["next_attempt_at"], now())
        self.assertEqual(first_row["last_error_code"], "gmail_auth_rejected")
        self.assertGreater(other_row["next_attempt_at"], now())
        self.assertEqual(other_row["last_error_code"], "temporary_failure")
        self.assertEqual(retry_audits, 1)
        delivered: list[str] = []
        batch = self.owner.dispatch_outbox(
            lambda address, *, event_id, event_type=None: (
                delivered.append(address) or MailResult(True, "accepted", provider_id="synthetic")
            ),
            limit=2, admin_email="admin@example.com",
        )
        self.assertEqual(batch["sent"], 1)
        self.assertEqual(delivered, ["admin@example.com"])
        with self.owner.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT status FROM notification_outbox WHERE id=?", (first,)
            ).fetchone()["status"], "sent")

    def test_email_diagnostic_is_admin_only_private_and_rate_limited(self) -> None:
        self.register_student()
        self.owner.bootstrap_admin(None, "Admin", "SyntheticPassword123!", "physicsplus")
        login = self.owner.handle(
            "POST", "/api/auth/login", {"login": "physicsplus", "password": "SyntheticPassword123!"},
            self.origin_headers, "127.0.0.2",
        )
        self.assertEqual(login.status, 200)
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]
        csrf = login.body["csrf_token"]
        admin_headers = {**self.origin_headers, "cookie": cookie, "x-csrf-token": csrf}
        student = self.owner.handle(
            "POST", "/api/admin/notification-test", {},
            {**self.origin_headers, "cookie": self.student_cookie,
             "x-csrf-token": self.owner.handle("GET", "/api/me", None,
                                              {"host": "school.example", "cookie": self.student_cookie},
                                              "127.0.0.1").body["csrf_token"]},
            "127.0.0.1",
        )
        self.assertEqual(student.status, 403)
        self.assertEqual(self.owner.handle(
            "POST", "/api/admin/notification-test", {},
            {**self.origin_headers, "cookie": cookie}, "127.0.0.2",
        ).status, 403)
        with self.owner.connect() as conn:
            conn.execute("DELETE FROM notification_outbox")
            conn.execute("DELETE FROM inbox_notifications")
            before_sheet = [tuple(row) for row in conn.execute(
                "SELECT dataset,revision FROM sheet_sync_state ORDER BY dataset"
            )]
            before_students = conn.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0]

        with patch.dict(os.environ, {}, clear=True):
            unconfigured = self.owner.handle("POST", "/api/admin/notification-test", {}, admin_headers, "127.0.0.2")
        self.assertEqual((unconfigured.status, unconfigured.body["error"]["code"]),
                         (503, "email_unconfigured"))

        configuration = {
            "PH_GMAIL_USER": "school@gmail.com",
            "PH_GMAIL_APP_PASSWORD": "abcdefghijklmnop",
            "PH_ADMIN_NOTIFICATION_EMAIL": "admin@example.com",
        }
        with patch.dict(os.environ, configuration, clear=True):
            invalid_body = self.owner.handle(
                "POST", "/api/admin/notification-test", {"recipient": "other@example.com"},
                admin_headers, "127.0.0.2",
            )
            self.assertEqual(invalid_body.status, 400)
            queued = self.owner.handle("POST", "/api/admin/notification-test", {}, admin_headers, "127.0.0.2")
            repeat = self.owner.handle("POST", "/api/admin/notification-test", {}, admin_headers, "127.0.0.2")
        self.assertEqual((queued.status, queued.body), (202, {"queued": True}))
        self.assertEqual((repeat.status, repeat.body["error"]["code"]), (429, "rate_limited"))
        self.assertNotIn("admin@example.com", str(queued.body))
        with self.owner.connect() as conn:
            row = conn.execute(
                "SELECT n.event_type,n.recipient_kind,n.recipient_user_id,a.entity,a.action "
                "FROM notification_outbox n JOIN audit_events a ON a.id=n.audit_event_id"
            ).fetchone()
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0], 1)
            self.assertEqual([tuple(item) for item in conn.execute(
                "SELECT dataset,revision FROM sheet_sync_state ORDER BY dataset"
            )], before_sheet)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0],
                             before_students)
        self.assertEqual(tuple(row), ("email_test", "admin", None, "email_test", "request"))

        received = []
        def fake_send(address: str, *, event_id: str, event_type: str) -> MailResult:
            received.append((address, event_id, event_type))
            return MailResult(True, "accepted", provider_id="synthetic")
        self.assertEqual(self.owner.dispatch_outbox(fake_send, limit=2,
                                                    admin_email="admin@example.com")["sent"], 1)
        self.assertEqual(len(received), 1)
        self.assertEqual((received[0][0], received[0][2]), ("admin@example.com", "email_test"))
        self.assertEqual(self.owner.dispatch_outbox(fake_send, limit=2,
                                                    admin_email="admin@example.com")["sent"], 0)

    def test_old_outbox_schema_gets_first_attempt_column(self) -> None:
        with self.owner.connect() as conn:
            conn.execute("DROP TABLE notification_outbox")
            conn.execute("CREATE TABLE notification_outbox(id TEXT PRIMARY KEY,audit_event_id TEXT UNIQUE,"
                         "event_type TEXT,recipient_kind TEXT,recipient_user_id TEXT,status TEXT,"
                         "attempts INTEGER,next_attempt_at INTEGER,lease_token TEXT,lease_until INTEGER,"
                         "provider_id TEXT,last_error_code TEXT,created_at INTEGER,sent_at INTEGER)")
            migrate_outbox_schema(conn)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(notification_outbox)")}
        self.assertIn("first_attempt_at", columns)


class CronSecurityTests(unittest.TestCase):
    def test_retry_endpoint_requires_secret_and_never_accepts_message_body(self) -> None:
        secret = "s" * 32
        with patch.dict(os.environ, {"CRON_SECRET": secret}), patch.object(
            site, "_dispatch_notifications", return_value={"processed": 0, "sent": 0, "retrying": 0, "dead": 0}
        ) as dispatch:
            client = site.app.test_client()
            self.assertEqual(client.get("/api/cron/notifications").status_code, 401)
            self.assertEqual(client.get("/api/cron/notifications", headers={"Authorization": "Bearer wrong"}).status_code, 401)
            response = client.get("/api/cron/notifications", headers={"Authorization": "Bearer " + secret})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["batch"]["processed"], 0)
            dispatch.assert_called_once_with(limit=25)


if __name__ == "__main__":
    unittest.main()
