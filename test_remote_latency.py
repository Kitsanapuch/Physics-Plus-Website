"""Offline regressions for the remote login path and cold worker startup."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import sys
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

# The remote-driver wheels are provided by Vercel in production. The tests
# exercise the connection lifecycle against SQLite when those wheels are not
# installed on a contributor's machine.
try:
    import libsql  # noqa: F401
except ModuleNotFoundError:
    sys.modules["libsql"] = types.ModuleType("libsql")
try:
    import turso_serverless  # noqa: F401
except ModuleNotFoundError:
    transport_stub = types.ModuleType("turso_serverless")
    transport_stub.Row = sqlite3.Row
    transport_stub.connect = lambda *_args, **_kwargs: None
    sys.modules["turso_serverless"] = transport_stub

import remote
from backend import ApiError, STUDENT_CODE_MIGRATION, now, uid


class RemoteLoginLatencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "remote.sqlite3"
        self.connections = 0
        self.statements: list[str] = []

        def sqlite_transport(_url, *, auth_token=None, isolation_level=None):
            self.connections += 1
            connection = sqlite3.connect(self.database, isolation_level=isolation_level)
            connection.set_trace_callback(self.statements.append)
            return connection

        driver = patch.object(remote.turso_serverless, "connect", sqlite_transport)
        driver.start()
        self.addCleanup(driver.stop)
        self.owner = self._owner()
        self.owner.initialize_remote()
        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")

    def _owner(self):
        return remote.RemotePhysicsPlusApp(
            "turso://test.example", "synthetic-test-token", "https://school.example"
        )

    def _login(self, *, login="physicsplus", password="wrong", ip="192.0.2.1", owner=None):
        return (owner or self.owner).handle(
            "POST", "/api/auth/login", {"login": login, "password": password},
            {"host": "school.example", "origin": "https://school.example"}, ip,
        )

    def _attempts(self, key: str) -> int:
        digest = hmac.new(
            self.owner.auth_token.encode(), key.encode(), hashlib.sha256
        ).hexdigest()
        with self.owner.connect() as conn:
            row = conn.execute(
                "SELECT attempts FROM login_throttle WHERE key_hash=?", (digest,)
            ).fetchone()
        return 0 if row is None else row[0]

    def test_login_and_bad_password_use_one_connection(self) -> None:
        before = self.connections
        success = self._login(password="SafeTestPassword123!")
        self.assertEqual(success.status, 200, success.body)
        self.assertEqual(self.connections - before, 1)
        before = self.connections
        self.assertEqual(self._login().status, 401)
        self.assertEqual(self.connections - before, 1)
        with self.owner.connect() as conn:
            sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(sessions, 1)
        self.assertEqual(self._attempts("ip:192.0.2.1"), 2)
        self.assertEqual(self._attempts("login:physicsplus"), 2)

    def test_anonymous_me_and_protected_page_do_not_open_turso(self) -> None:
        before = self.connections
        self.assertEqual(self.owner.static_auth("/dashboard.html", ""), ("login", None))
        self.assertEqual(self.owner.static_auth("/dashboard.html", "pp_session=malformed"),
                         ("login", None))
        headers = {"host": "school.example"}
        me = self.owner.handle("GET", "/api/me", None, headers, "192.0.2.1")
        self.assertEqual(me.body, {"authenticated": False})
        other = self.owner.handle("GET", "/api/student/dashboard", None, headers, "192.0.2.1")
        self.assertEqual(other.status, 401)
        self.assertEqual(self.connections, before)

    def test_credential_submission_does_not_read_stale_cookie_session(self) -> None:
        first = self._login(password="SafeTestPassword123!")
        cookie = first.headers["Set-Cookie"].split(";", 1)[0]
        self.statements.clear()
        second = self.owner.handle(
            "POST", "/api/auth/login",
            {"login": "physicsplus", "password": "SafeTestPassword123!"},
            {"host": "school.example", "origin": "https://school.example", "cookie": cookie},
            "192.0.2.1",
        )
        self.assertEqual(second.status, 200)
        self.assertFalse(any("FROM sessions s JOIN users u" in statement
                             for statement in self.statements))

    def test_warm_connection_enables_foreign_keys_without_rechecking_driver(self) -> None:
        self.statements.clear()
        with self.owner.connect() as conn:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        statements = [statement.strip().upper() for statement in self.statements]
        self.assertIn("PRAGMA FOREIGN_KEYS=ON", statements)
        self.assertNotIn("PRAGMA FOREIGN_KEYS", statements)
        with self.owner.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_expired_sessions_are_cleaned_by_insert_without_extra_round_trip(self) -> None:
        with self.owner.connect() as conn:
            admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                         ("expired-token", admin_id, "expired-csrf", 1, 2))
        self.statements.clear()
        login = self._login(password="SafeTestPassword123!")
        self.assertEqual(login.status, 200, login.body)
        self.assertFalse(any(statement.strip().upper().startswith(
            "DELETE FROM SESSIONS WHERE EXPIRES_AT") for statement in self.statements))
        with self.owner.connect() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM sessions WHERE token_hash='expired-token'").fetchone())

    def test_cold_worker_installs_missing_session_trigger_once(self) -> None:
        with self.owner.connect() as conn:
            conn.execute("DROP TRIGGER sessions_cleanup_expired")
        cold = self._owner()
        with cold.connect() as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' "
                "AND name='sessions_cleanup_expired'"
            ).fetchone())
        self.statements.clear()
        with cold.connect() as conn:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        self.assertFalse(any(statement.strip().upper().startswith("CREATE TRIGGER")
                             for statement in self.statements))

    def test_ip_and_identifier_limits_still_count_independently(self) -> None:
        for _ in range(10):
            self.assertEqual(self._login().status, 401)
        self.assertEqual(self._login().status, 429)
        self.assertEqual(self._attempts("ip:192.0.2.1"), 11)
        self.assertEqual(self._attempts("login:physicsplus"), 10)

        for _ in range(9):
            self.assertEqual(self._login(login="anotheruser").status, 401)
        self.assertEqual(self._attempts("ip:192.0.2.1"), 20)
        self.assertEqual(self._login(login="anotheruser").status, 429)
        self.assertEqual(self._attempts("ip:192.0.2.1"), 20)
        self.assertEqual(self._attempts("login:anotheruser"), 9)

        self.assertEqual(self._login(ip="192.0.2.2").status, 429)
        self.assertEqual(self._attempts("ip:192.0.2.2"), 1)
        self.assertEqual(self._attempts("login:physicsplus"), 10)

    def test_login_limits_share_one_atomic_remote_write(self) -> None:
        self.statements.clear()
        self.owner.limiter.check_login_attempt("192.0.2.50", "newuser")
        throttle_writes = [statement for statement in self.statements
                           if "INSERT INTO login_throttle" in statement]
        self.assertEqual(len(throttle_writes), 1, throttle_writes)
        self.assertEqual(self._attempts("ip:192.0.2.50"), 1)
        self.assertEqual(self._attempts("login:newuser"), 1)

    def test_last_available_ip_attempt_also_counts_identifier(self) -> None:
        for index in range(19):
            self.owner.limiter.check_login_attempt("192.0.2.51", f"user{index}")
        self.owner.limiter.check_login_attempt("192.0.2.51", "finaluser")
        self.assertEqual(self._attempts("ip:192.0.2.51"), 20)
        self.assertEqual(self._attempts("login:finaluser"), 1)
        with self.assertRaises(ApiError) as blocked:
            self.owner.limiter.check_login_attempt("192.0.2.51", "anotheruser")
        self.assertEqual(blocked.exception.status, 429)
        self.assertEqual(self._attempts("ip:192.0.2.51"), 20)
        self.assertEqual(self._attempts("login:anotheruser"), 0)

    def test_parallel_login_limits_cannot_exceed_identifier_limit(self) -> None:
        def attempt(_index: int) -> int:
            try:
                self.owner.limiter.check_login_attempt("192.0.2.52", "paralleluser")
                return 200
            except ApiError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(attempt, range(14)))
        self.assertEqual(statuses.count(200), 10)
        self.assertEqual(statuses.count(429), 4)
        self.assertEqual(self._attempts("ip:192.0.2.52"), 14)
        self.assertEqual(self._attempts("login:paralleluser"), 10)

    def test_malformed_identifier_counts_only_the_ip_attempt(self) -> None:
        malformed_email = self._login(login="a@", ip="192.0.2.30")
        malformed_username = self._login(login="!bad", ip="192.0.2.30")
        self.assertEqual(malformed_email.status, 400)
        self.assertEqual(malformed_username.status, 400)
        self.assertEqual(self._attempts("ip:192.0.2.30"), 2)
        self.assertEqual(self._attempts("login:a@"), 0)
        self.assertEqual(self._attempts("login:!bad"), 0)

    def test_throttle_window_resets_at_exact_expiry(self) -> None:
        key = "login:window@example.com"
        with patch.object(remote, "now", return_value=1000):
            self.owner.limiter.check(key, 2, 10)
            self.owner.limiter.check(key, 2, 10)
        with patch.object(remote, "now", return_value=1009):
            with self.assertRaises(ApiError) as blocked:
                self.owner.limiter.check(key, 2, 10)
        self.assertEqual(blocked.exception.status, 429)
        self.assertEqual(self._attempts(key), 2)
        with patch.object(remote, "now", return_value=1010):
            self.owner.limiter.check(key, 2, 10)
        self.assertEqual(self._attempts(key), 1)

    def test_throttle_count_survives_later_transaction_rollback(self) -> None:
        key = "login:durable@example.com"
        with self.assertRaises(RuntimeError):
            with self.owner.connect() as conn:
                self.owner.limiter.check(key, 2, 900, conn=conn)
                conn.execute("BEGIN IMMEDIATE")
                raise RuntimeError("authentication failed after throttle check")
        self.assertEqual(self._attempts(key), 1)

    def test_parallel_attempts_cannot_exceed_shared_limit(self) -> None:
        key = "login:parallel@example.com"

        def attempt(_index: int) -> int:
            try:
                self.owner.limiter.check(key, 10, 900)
                return 200
            except ApiError as exc:
                return exc.status

        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(attempt, range(12)))
        self.assertEqual(statuses.count(200), 10)
        self.assertEqual(statuses.count(429), 2)
        self.assertEqual(self._attempts(key), 10)

    def test_cold_worker_uses_read_only_schema_checks(self) -> None:
        cold = self._owner()
        self.statements.clear()
        with cold.connect() as conn:
            self.assertEqual(conn.execute("SELECT 1").fetchone()[0], 1)
        statements = [statement.strip().upper() for statement in self.statements]
        self.assertTrue(any("APP_MIGRATIONS WHERE KEY=" in s for s in statements))
        self.assertFalse(any(s.startswith(("CREATE ", "INSERT ", "UPDATE ", "BEGIN IMMEDIATE"))
                         for s in statements), statements)

    def test_cold_worker_applies_pending_student_code_migration(self) -> None:
        with self.owner.connect() as conn:
            admin_hash = conn.execute(
                "SELECT password_hash FROM users WHERE role='admin'"
            ).fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO users(id,email,public_code,name,password_hash,role,created_at) "
                "VALUES(?,?,?,?,?,'student',?)",
                (uid(), "student@example.com", "PP-S-000001", "Student", admin_hash, now()),
            )
            conn.execute("DELETE FROM app_migrations WHERE key=?", (STUDENT_CODE_MIGRATION,))
            conn.commit()
        cold = self._owner()
        with cold.connect() as conn:
            code = conn.execute(
                "SELECT public_code FROM users WHERE email='student@example.com'"
            ).fetchone()[0]
            marker = conn.execute(
                "SELECT 1 FROM app_migrations WHERE key=?", (STUDENT_CODE_MIGRATION,)
            ).fetchone()
        self.assertRegex(code, r"^[0-9]{5}$")
        self.assertIsNotNone(marker)

    def test_compact_admin_dashboard_keeps_shared_data_and_full_api(self) -> None:
        login = self._login(password="SafeTestPassword123!")
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]
        headers = {"host": "school.example", "cookie": cookie}
        self.statements.clear()
        compact = self.owner.handle("GET", "/api/admin/dashboard?compact=1", None, headers, "192.0.2.1")
        compact_statements = list(self.statements)
        full = self.owner.handle("GET", "/api/admin/dashboard", None, headers, "192.0.2.1")
        self.assertEqual(compact.status, 200)
        self.assertEqual(full.status, 200)
        for key in ("pending_time_entries", "pending_payment_claims", "notifications"):
            self.assertEqual(compact.body[key], full.body[key])
        self.assertEqual(compact.body["invoices"], [])
        self.assertFalse(any("FROM INVOICES I JOIN USERS U" in statement.upper()
                             for statement in compact_statements))
        self.assertNotIn("users", compact.body)
        self.assertNotIn("staff", compact.body)
        self.assertIn("users", full.body)
        self.assertIn("staff", full.body)

    def test_protected_page_bootstrap_uses_existing_session_permissions(self) -> None:
        self.assertEqual(self.owner.static_auth("/dashboard.html", ""), ("login", None))
        login = self._login(password="SafeTestPassword123!")
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]
        access, payload = self.owner.static_auth("/dashboard.html", cookie)
        self.assertIsNone(access)
        self.assertEqual(payload["user"]["role"], "admin")
        self.assertEqual(payload["csrf_token"], login.body["csrf_token"])
        self.assertEqual(self.owner.static_auth("/student", cookie), ("forbidden", None))

        self.statements.clear()
        access, bootstrapped = self.owner.static_auth(
            "/dashboard.html", cookie, include_overview=True
        )
        self.assertIsNone(access)
        self.assertEqual(bootstrapped["overview_counts"]["pending_claims"], 0)
        self.assertEqual(bootstrapped["user"], payload["user"])
        self.assertFalse(any(statement.strip().upper().startswith(("INSERT ", "UPDATE ", "DELETE "))
                             for statement in self.statements), self.statements)
        with patch.object(self.owner, "_admin_overview", side_effect=RuntimeError("temporary reporting failure")):
            access, fallback = self.owner.static_auth(
                "/dashboard.html", cookie, include_overview=True
            )
        self.assertIsNone(access)
        self.assertEqual(fallback["user"], payload["user"])
        self.assertNotIn("overview_counts", fallback)

    def test_fast_admin_overview_reports_current_counts(self) -> None:
        login = self._login(password="SafeTestPassword123!")
        cookie = login.headers["Set-Cookie"].split(";", 1)[0]
        summary = self.owner.handle("GET", "/api/admin/overview", None,
                                    {"host": "school.example", "cookie": cookie}, "192.0.2.1")
        self.assertEqual(summary.status, 200, summary.body)
        self.assertEqual(summary.body, {
            "invoice_count": 0, "outstanding_satang": 0, "overdue_count": 0,
            "pending_claims": 0, "pending_times": 0, "unbilled_count": 0,
            "failed_notices": 0,
        })

    def test_admin_overview_counts_older_invoices_and_all_unbilled_courses(self) -> None:
        with self.owner.connect() as conn:
            admin = conn.execute("SELECT id,password_hash FROM users WHERE role='admin'").fetchone()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO users(id,email,public_code,name,password_hash,role,created_at) "
                "VALUES(?,?,?,?,?,'student',?)",
                ("student-many", "many@example.com", "26001", "Student", admin["password_hash"], now()),
            )
            for index in range(501):
                course_id = f"course-{index}"
                conn.execute(
                    "INSERT INTO courses(id,title,subject,level,price_satang,capacity,created_at) "
                    "VALUES(?,?,?,'middle',10000,13,?)",
                    (course_id, f"Course {index}", "math", index),
                )
                conn.execute(
                    "INSERT INTO enrollments(id,course_id,student_id,created_at) VALUES(?,?,?,?)",
                    (f"enrollment-{index}", course_id, "student-many", index),
                )
                conn.execute(
                    "INSERT INTO invoices(id,invoice_number,student_id,description,amount_satang,"
                    "status,due_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (f"invoice-{index}", f"INV-{index}", "student-many", "Tuition", 10000,
                     "unpaid" if index == 0 else "void", "2020-01-01", index),
                )
            conn.execute(
                "INSERT INTO payments(id,invoice_id,amount_satang,method,reference,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("payment-oldest", "invoice-0", 2500, "bank_transfer", "OLD-1", admin["id"], now()),
            )
            conn.commit()
        with self.owner.connect() as conn:
            result = self.owner._admin_overview(conn)
        self.assertEqual(result.body["invoice_count"], 501)
        self.assertEqual(result.body["outstanding_satang"], 7500)
        self.assertEqual(result.body["overdue_count"], 1)
        self.assertEqual(result.body["unbilled_count"], 501)


if __name__ == "__main__":
    unittest.main()
