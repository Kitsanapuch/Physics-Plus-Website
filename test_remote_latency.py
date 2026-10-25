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


if __name__ == "__main__":
    unittest.main()
