"""Student public IDs stay short, chronological, and separate from database keys."""

from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import (ApiError, PhysicsPlusApp, STUDENT_CODE_MIGRATION,
                     migrate_student_codes, next_student_code)


class StudentCodeTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.owner = PhysicsPlusApp(Path(directory.name) / "school.sqlite3",
                                    "https://school.example")
        self.headers = {"host": "school.example", "origin": "https://school.example"}

    def register(self, email: str):
        result = self.owner.handle("POST", "/api/auth/register", {
            "name": "Example Student", "email": email,
            "password": "SyntheticPassword123!", "grade": "M4", "school": "School",
            "guardian_name": "Guardian", "guardian_phone": "0812345678",
            "guardian_relationship": "mother",
        }, self.headers, "127.0.0.1")
        self.assertEqual(result.status, 201, result.body)
        return result.body["user"]

    def test_registration_uses_five_digits_and_resets_sequence_each_year(self):
        with patch("backend.student_code_year", return_value="26"):
            first = self.register("first@example.com")
            second = self.register("second@example.com")
        with patch("backend.student_code_year", return_value="27"):
            third = self.register("third@example.com")
        self.assertEqual([first["student_code"], second["student_code"],
                          third["student_code"]], ["26001", "26002", "27001"])
        self.assertTrue(all(re.fullmatch(r"[0-9]{5}", user["student_code"])
                            for user in (first, second, third)))
        self.assertEqual(len({user["id"] for user in (first, second, third)}), 3)

    def test_exhausted_year_fails_instead_of_reusing_a_code(self):
        with self.owner.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT OR REPLACE INTO code_counters(prefix,next_number) "
                         "VALUES('student:26',1000)")
            with self.assertRaises(ApiError) as failure:
                next_student_code(conn, year="26")
            self.assertEqual(failure.exception.code, "student_code_capacity_exhausted")
            conn.rollback()

    def test_admin_created_student_shares_the_same_year_sequence(self):
        self.owner.bootstrap_admin(None, "Owner", "SafeTestPassword123!", "owner")
        login = self.owner.handle("POST", "/api/auth/login", {
            "login": "owner", "password": "SafeTestPassword123!",
        }, self.headers, "127.0.0.1")
        self.assertEqual(login.status, 200)
        auth = {**self.headers,
                "cookie": login.headers["Set-Cookie"].split(";", 1)[0],
                "x-csrf-token": login.body["csrf_token"]}
        with patch("backend.student_code_year", return_value="26"):
            first = self.register("self-signup@example.com")
            created = self.owner.handle("POST", "/api/admin/users", {
                "name": "Admin Created", "email": "admin-created@example.com",
                "role": "student",
            }, auth, "127.0.0.2")
        self.assertEqual(created.status, 201, created.body)
        self.assertEqual(first["student_code"], "26001")
        self.assertEqual(created.body["user"]["student_code"], "26002")

    def test_legacy_display_code_migration_preserves_relationships_and_queues_sheet(self):
        student = self.register("legacy@example.com")
        student_id = student["id"]
        with self.owner.connect() as conn:
            course_id = conn.execute("SELECT id FROM courses LIMIT 1").fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO enrollments(id,course_id,student_id,created_at) "
                         "VALUES('enrollment-1',?,?,1)", (course_id, student_id))
            conn.execute("UPDATE users SET public_code='PP-S-000001' WHERE id=?", (student_id,))
            conn.execute("DELETE FROM app_migrations WHERE key=?", (STUDENT_CODE_MIGRATION,))
            conn.execute("UPDATE sheet_sync_state SET synced_revision=revision")
            conn.commit()

            self.assertTrue(migrate_student_codes(conn))
            changed = conn.execute("SELECT id,public_code FROM users WHERE id=?",
                                   (student_id,)).fetchone()
            self.assertEqual(changed["id"], student_id)
            self.assertRegex(changed["public_code"], r"^[0-9]{5}$")
            self.assertEqual(conn.execute("SELECT student_id FROM enrollments "
                                          "WHERE id='enrollment-1'").fetchone()[0], student_id)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertGreater(conn.execute("SELECT COUNT(*) FROM sheet_sync_state "
                                                  "WHERE revision>synced_revision").fetchone()[0], 0)
            self.assertFalse(migrate_student_codes(conn))


if __name__ == "__main__":
    unittest.main()
