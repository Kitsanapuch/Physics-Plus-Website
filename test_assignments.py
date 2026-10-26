"""Role isolation, state changes and revision polling for assigned work."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from backend import PhysicsPlusApp, hash_password, now, uid


class AssignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = PhysicsPlusApp(Path(temporary.name) / "school.sqlite3", "https://school.example")
        self.base = {"host": "school.example", "origin": "https://school.example"}
        self.app.bootstrap_admin(None, "Administrator", "SyntheticAdminPassword123!", "testadmin")
        self.admin = self.login("testadmin", "SyntheticAdminPassword123!")
        self.student_id, self.student = self.register_student("student@example.com")
        self.other_id, self.other = self.register_student("other@example.com")
        self.teacher_id = uid()
        with self.app.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO users(id,email,public_code,name,password_hash,role,created_at) "
                "VALUES(?,?,?,?,?,'teacher',?)",
                (self.teacher_id, "teacher@example.com", "PP-T-009999", "Teacher",
                 hash_password("SyntheticTeacherPassword123!"), now()),
            )
            conn.commit()
        self.teacher = self.login("teacher@example.com", "SyntheticTeacherPassword123!")

    def login(self, username: str, password: str) -> dict[str, str]:
        result = self.app.handle("POST", "/api/auth/login",
                                 {"login": username, "password": password},
                                 self.base, "127.0.0.1")
        self.assertEqual(result.status, 200, result.body)
        return {**self.base,
                "cookie": result.headers["Set-Cookie"].split(";", 1)[0],
                "x-csrf-token": result.body["csrf_token"]}

    def register_student(self, email: str) -> tuple[str, dict[str, str]]:
        result = self.app.handle(
            "POST", "/api/auth/register",
            {"name": email.split("@", 1)[0], "email": email,
             "password": "SyntheticStudentPassword123!", "grade": "M4",
             "school": "School", "guardian_name": "Guardian",
             "guardian_phone": "0812345678", "guardian_relationship": "mother"},
            self.base, "127.0.0.1",
        )
        self.assertEqual(result.status, 201, result.body)
        headers = {**self.base,
                   "cookie": result.headers["Set-Cookie"].split(";", 1)[0],
                   "x-csrf-token": result.body["csrf_token"]}
        return result.body["user"]["id"], headers

    def request(self, actor: dict[str, str], method: str, path: str,
                data: dict | None = None):
        return self.app.handle(method, path, data, actor, "127.0.0.1")

    def test_revision_requires_login_and_changes_only_on_business_audit(self) -> None:
        anonymous = self.app.handle("GET", "/api/sync/revision", None, self.base, "127.0.0.1")
        self.assertEqual(anonymous.status, 401)
        before = self.request(self.student, "GET", "/api/sync/revision")
        self.assertEqual(before.status, 200)
        self.assertIsInstance(before.body["revision"], int)
        self.assertEqual(self.request(self.student, "GET", "/api/sync/revision").body, before.body)
        created = self.request(self.admin, "POST", "/api/admin/tasks",
                               {"assignee_id": self.student_id, "title": "Review chapter 1"})
        self.assertEqual(created.status, 201, created.body)
        after = self.request(self.student, "GET", "/api/sync/revision")
        self.assertGreater(after.body["revision"], before.body["revision"])

    def test_student_only_sees_and_completes_own_work(self) -> None:
        created = self.request(self.admin, "POST", "/api/admin/tasks", {
            "assignee_id": self.student_id, "title": "Read vectors",
            "description": "Pages 2–4", "due_date": "2026-11-01",
        })
        self.assertEqual(created.status, 201, created.body)
        task_id = created.body["task"]["id"]
        own = self.request(self.student, "GET", "/api/tasks")
        self.assertEqual(own.status, 200)
        self.assertEqual(own.body["open_count"], 1)
        self.assertEqual(own.body["tasks"][0]["id"], task_id)
        self.assertEqual(self.request(self.other, "GET", "/api/tasks").body["tasks"], [])
        self.assertEqual(self.request(self.teacher, "GET", "/api/tasks").body["tasks"], [])
        self.assertEqual(self.request(self.other, "POST", f"/api/tasks/{task_id}/complete", {}).status, 404)
        self.assertEqual(self.request(self.student, "GET", "/api/admin/tasks").status, 403)
        self.assertEqual(self.request(self.student, "POST", "/api/admin/tasks",
                                      {"assignee_id": self.student_id, "title": "Forged"}).status, 403)
        done = self.request(self.student, "POST", f"/api/tasks/{task_id}/complete", {})
        self.assertEqual(done.status, 200, done.body)
        self.assertEqual(done.body["task"]["status"], "completed")
        self.assertIsNotNone(done.body["task"]["completed_at"])
        self.assertEqual(self.request(self.student, "POST", f"/api/tasks/{task_id}/complete", {}).status, 200)
        self.assertEqual(self.request(self.admin, "GET", "/api/admin/tasks").body["completed_count"], 1)
        notices = self.request(self.admin, "GET", "/api/notifications").body["notifications"]
        self.assertIn("task_completed", {row["event_type"] for row in notices})
        with self.app.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM audit_events WHERE entity='assignment' AND action='complete'").fetchone()[0], 1)

    def test_admin_reassignment_cancel_and_direct_notifications(self) -> None:
        created = self.request(self.admin, "POST", "/api/admin/tasks",
                               {"assignee_id": self.student_id, "title": "Original"})
        task_id = created.body["task"]["id"]
        edited = self.request(self.admin, "PUT", f"/api/admin/tasks/{task_id}", {
            "assignee_id": self.teacher_id, "title": "New teacher task", "due_date": "2026-11-03",
        })
        self.assertEqual(edited.status, 200, edited.body)
        self.assertEqual(edited.body["task"]["assignee_id"], self.teacher_id)
        self.assertEqual(self.request(self.student, "GET", "/api/tasks").body["tasks"], [])
        self.assertEqual(self.request(self.teacher, "GET", "/api/tasks").body["tasks"][0]["id"], task_id)
        student_events = {row["event_type"] for row in self.request(self.student, "GET", "/api/notifications").body["notifications"]}
        teacher_events = {row["event_type"] for row in self.request(self.teacher, "GET", "/api/notifications").body["notifications"]}
        self.assertIn("task_unassigned", student_events)
        self.assertIn("task_assigned", teacher_events)
        self.assertEqual(self.request(self.student, "POST", f"/api/tasks/{task_id}/complete", {}).status, 404)
        cancelled = self.request(self.admin, "POST", f"/api/admin/tasks/{task_id}/cancel", {})
        self.assertEqual(cancelled.status, 200)
        self.assertEqual(cancelled.body["task"]["status"], "cancelled")
        self.assertEqual(self.request(self.teacher, "POST", f"/api/tasks/{task_id}/complete", {}).status, 409)
        self.assertEqual(self.request(self.admin, "PUT", f"/api/admin/tasks/{task_id}", {"title": "Late edit"}).status, 409)

    def test_task_validates_real_calendar_date_account_and_course_membership(self) -> None:
        physics = next(row for row in self.request(self.admin, "GET", "/api/courses").body["courses"]
                       if row["subject"] == "physics")
        for payload in (
            {"assignee_id": self.student_id, "title": "Bad date", "due_date": "2026-02-30"},
            {"assignee_id": "0" * 32, "title": "No person"},
            {"assignee_id": self.student_id, "title": "Wrong course", "course_id": physics["id"]},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.request(self.admin, "POST", "/api/admin/tasks", payload).status, 400)
        enrolled = self.request(self.student, "POST", "/api/student/enrollments", {
            "course_id": physics["id"], "grade": "M4", "school": "School",
            "guardian_name": "Guardian", "guardian_phone": "0812345678",
            "guardian_relationship": "mother",
        })
        self.assertEqual(enrolled.status, 201, enrolled.body)
        created = self.request(self.admin, "POST", "/api/admin/tasks", {
            "assignee_id": self.student_id, "title": "Physics exercise", "course_id": physics["id"],
        })
        self.assertEqual(created.status, 201, created.body)
        self.assertEqual(created.body["task"]["course_title"], physics["title"])
        report = self.request(self.admin, "GET", "/api/admin/reports/assignments")
        self.assertEqual(report.status, 200, report.body)
        self.assertEqual(report.body["rows"][0]["id"], created.body["task"]["id"])
        self.assertIn("assignee_code", report.body["rows"][0])
        with self.app.connect() as conn:
            state = conn.execute("SELECT revision,synced_revision FROM sheet_sync_state WHERE dataset='assignments'").fetchone()
        self.assertGreater(state["revision"], state["synced_revision"])


if __name__ == "__main__":
    unittest.main()
