"""Exercise the Marketplace libSQL path with a local libsql database only."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import libsql

import app as site
import manage
import remote
from sheets_sync import dispatch


class MarketplaceLibsqlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        database = str(Path(self.temp.name) / "marketplace-stand-in.sqlite3")
        self.driver_calls = []
        original_connect = libsql.connect

        def local_transport(*, database: str, auth_token: str, isolation_level, timeout: float):
            self.driver_calls.append((database, auth_token, isolation_level, timeout))
            return original_connect(self.database, isolation_level=isolation_level, timeout=timeout)

        self.database = database
        connection_patch = patch.object(remote.libsql, "connect", local_transport)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)
        self.owner = remote.RemotePhysicsPlusApp(
            "libsql://test.example", "synthetic-test-token", "https://school.example"
        )
        self.owner.initialize_remote()

    def test_schema_rows_transactions_and_driver_routing(self) -> None:
        self.assertEqual(self.driver_calls[0], (
            "libsql://test.example", "synthetic-test-token", None, 30.0
        ))
        status = self.owner.check_remote()
        self.assertTrue(status["schema_initialized"])
        self.assertTrue(status["default_rate_grid_matches"])
        self.assertEqual(status["confirmed_default_rate_count"], 8)

        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")
        with self.owner.connect() as conn:
            row = conn.execute("SELECT id,username FROM users WHERE role='admin'").fetchone()
            self.assertEqual(row["username"], "physicsplus")
            self.assertEqual(row["USERNAME"], "physicsplus")
            self.assertEqual(row[1], "physicsplus")
            self.assertEqual(dict(row), {"id": row[0], "username": "physicsplus"})
            self.assertEqual([item["username"] for item in conn.execute(
                "SELECT username FROM users WHERE role='admin'"
            )], ["physicsplus"])
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM courses c JOIN course_formats f ON f.course_id=c.id "
                "WHERE f.format='individual'"
            ).fetchone()[0], 8)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM courses WHERE active=1"
            ).fetchone()[0], 16)

        with self.assertRaises(RuntimeError):
            with self.owner.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO login_throttle(key_hash,window_started,attempts) VALUES('rollback',1,1)"
                )
                raise RuntimeError("roll back the write")
        with self.owner.connect() as conn:
            self.assertIsNone(conn.execute(
                "SELECT key_hash FROM login_throttle WHERE key_hash='rollback'"
            ).fetchone())

    def test_sheet_delivery_reuses_one_native_connection(self) -> None:
        class Recorder:
            def __init__(self):
                self.snapshots = None

            def write(self, snapshots):
                self.snapshots = snapshots

        writer = Recorder()
        with patch.object(self.owner, "connect", wraps=self.owner.connect) as connect:
            result = dispatch(self.owner, client=writer)
        self.assertEqual(connect.call_count, 1)
        self.assertGreater(result["synced"], 0)
        self.assertEqual(result["processed"], result["synced"])
        self.assertIn("courses", writer.snapshots)

    def test_constraint_errors_and_script_errors_are_not_hidden(self) -> None:
        with self.owner.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO login_throttle VALUES('unique',1,1)")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("INSERT INTO login_throttle VALUES('unique',1,1)")
            conn.rollback()
            with self.assertRaises(ValueError):
                conn.executescript("CREATE TABLE IF NOT EXISTS script_probe(x); INVALID SQL;")

    def test_http_login_and_registration_use_libsql_rows(self) -> None:
        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")
        with patch.object(site, "_backend", return_value=self.owner):
            admin = site.app.test_client()
            login = admin.post(
                "/api/auth/login", base_url="https://school.example",
                headers={"Origin": "https://school.example"},
                json={"login": "physicsplus", "password": "SafeTestPassword123!"},
            )
            self.assertEqual(login.status_code, 200, login.json)
            self.assertEqual(login.json["user"]["role"], "admin")
            self.assertEqual(admin.get(
                "/api/admin/reports/students", base_url="https://school.example"
            ).status_code, 200)

            student = site.app.test_client()
            registration = student.post(
                "/api/auth/register", base_url="https://school.example",
                headers={"Origin": "https://school.example"},
                json={
                    "name": "Example Student", "email": "student@example.com",
                    "password": "SafeTestPassword123!", "grade": "M3",
                    "school": "Example School", "guardian_name": "Example Guardian",
                    "guardian_phone": "0812345678", "guardian_relationship": "mother",
                },
            )
            self.assertEqual(registration.status_code, 201, registration.json)
            self.assertEqual(student.get(
                "/api/me", base_url="https://school.example"
            ).json["user"]["role"], "student")

    def test_native_libsql_installs_session_cleanup_on_cold_worker(self) -> None:
        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")
        with self.owner.connect() as conn:
            conn.execute("DROP TRIGGER sessions_cleanup_expired")
        cold = remote.RemotePhysicsPlusApp(
            "libsql://test.example", "synthetic-test-token", "https://school.example"
        )
        with cold.connect() as conn:
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='sessions_cleanup_expired'"
            ).fetchone())
            admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
                         ("expired-native-token", admin_id, "expired-csrf", 1, 2))
        login = cold.handle(
            "POST", "/api/auth/login",
            {"login": "physicsplus", "password": "SafeTestPassword123!"},
            {"host": "school.example", "origin": "https://school.example"}, "192.0.2.1"
        )
        self.assertEqual(login.status, 200, login.body)
        with cold.connect() as conn:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM sessions WHERE token_hash='expired-native-token'"
            ).fetchone())

    def test_readiness_requires_active_admin_and_exact_rate_grid(self) -> None:
        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")
        self.assertEqual(self.owner.check_remote()["active_admin_count"], 1)
        with self.owner.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE users SET status='disabled' WHERE role='admin'")
            conn.commit()
        status = self.owner.check_remote()
        self.assertEqual(status["admin_count"], 1)
        self.assertEqual(status["active_admin_count"], 0)
        with patch.object(manage, "backend", return_value=self.owner), patch(
            "sys.argv", ["manage.py", "check"]
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(manage.main(), 1)

        with self.owner.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE users SET status='active' WHERE role='admin'")
            conn.execute("UPDATE rate_rules SET rate_satang_per_hour=rate_satang_per_hour+1 "
                         "WHERE teacher_id IS NULL AND state='confirmed' AND level='middle' "
                         "AND min_students=1")
            conn.commit()
        status = self.owner.check_remote()
        self.assertEqual(status["confirmed_default_rate_count"], 8)
        self.assertFalse(status["default_rate_grid_matches"])
        with patch.object(manage, "backend", return_value=self.owner), patch(
            "sys.argv", ["manage.py", "check"]
        ), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(manage.main(), 1)

    def test_url_rejects_inline_credentials_and_nonremote_schemes(self) -> None:
        for url in (
            "libsql://user@test.example", "libsql://test.example/path",
            "libsql://test.example?authToken=secret", "file:local.db",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                remote.RemotePhysicsPlusApp(url, "synthetic-test-token", "https://school.example")


if __name__ == "__main__":
    unittest.main()
