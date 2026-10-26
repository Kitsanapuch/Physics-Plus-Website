"""Regression checks for the small polling endpoint and real free teacher time."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import PhysicsPlusApp


PASSWORD = "SyntheticQAPassword123!"
ZONE = timezone(timedelta(hours=7))


class BackendQATests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = PhysicsPlusApp(Path(temporary.name) / "qa.sqlite3", "https://school.example")
        self.origin = {"host": "school.example", "origin": "https://school.example"}
        self.app.bootstrap_admin(None, "QA Admin", PASSWORD, "qaadmin")
        self.admin = self.login("qaadmin")
        student = self.request(self.origin, "POST", "/api/auth/register", {
            "name": "QA Student", "email": "qa.student@example.com", "password": PASSWORD,
            "grade": "M4", "school": "QA School", "guardian_name": "Guardian",
            "guardian_phone": "0812345678", "guardian_relationship": "mother",
        })
        self.assertEqual(student.status, 201, student.body)
        self.student_id = student.body["user"]["id"]
        self.student = self.auth_from_response(student)

    def request(self, auth: dict[str, str], method: str, path: str,
                data: dict | None = None):
        return self.app.handle(method, path, data, auth, "127.0.0.1")

    def auth_from_response(self, response) -> dict[str, str]:
        return {**self.origin,
                "cookie": response.headers["Set-Cookie"].split(";", 1)[0],
                "x-csrf-token": response.body["csrf_token"]}

    def login(self, login: str) -> dict[str, str]:
        response = self.request(self.origin, "POST", "/api/auth/login",
                                {"login": login, "password": PASSWORD})
        self.assertEqual(response.status, 200, response.body)
        return self.auth_from_response(response)

    def test_revision_stays_unchanged_for_login_but_changes_for_business_write(self) -> None:
        before = self.request(self.student, "GET", "/api/sync/revision")
        self.assertEqual(before.status, 200)
        self.login("qaadmin")
        self.assertEqual(self.request(self.student, "GET", "/api/sync/revision").body,
                         before.body)
        created = self.request(self.admin, "POST", "/api/admin/tasks",
                               {"assignee_id": self.student_id, "title": "Read unit 1"})
        self.assertEqual(created.status, 201, created.body)
        after = self.request(self.student, "GET", "/api/sync/revision")
        self.assertGreater(after.body["revision"], before.body["revision"])

    def test_retrying_same_notice_does_not_duplicate_personal_inbox(self) -> None:
        with self.app.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            event = self.app.audit(conn, self.student_id, "assignment", "test-event", "create")
            self.app.enqueue_notice(conn, event, "task_assigned", self.student_id)
            self.app.enqueue_notice(conn, event, "task_assigned", self.student_id)
            conn.commit()
        student_inbox = self.request(self.student, "GET", "/api/notifications")
        self.assertEqual(student_inbox.status, 200)
        self.assertEqual(sum(item["event_type"] == "task_assigned"
                             for item in student_inbox.body["notifications"]), 1)
        with self.app.connect() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE audit_event_id=?", (event,)
            ).fetchone()[0], 1)

    def test_invoice_claim_and_receipt_are_private_to_purchasing_student(self) -> None:
        course = next(row for row in self.request(self.student, "GET", "/api/courses").body["courses"]
                      if row["level"] == "high")
        purchase = self.request(self.student, "POST", "/api/student/enrollments", {
            "course_id": course["id"], "grade": "M4", "school": "QA School",
            "guardian_name": "Guardian", "guardian_phone": "0812345678",
            "guardian_relationship": "mother", "confirm_purchase": True,
        })
        self.assertEqual(purchase.status, 201, purchase.body)
        invoice_id = purchase.body["invoice_id"]
        other = self.request(self.origin, "POST", "/api/auth/register", {
            "name": "Other Student", "email": "other.qa@example.com", "password": PASSWORD,
            "grade": "M4", "school": "QA School", "guardian_name": "Guardian",
            "guardian_phone": "0812345678", "guardian_relationship": "mother",
        })
        self.assertEqual(other.status, 201, other.body)
        outsider = self.auth_from_response(other)
        self.assertNotIn(invoice_id,
                         {row["id"] for row in self.request(outsider, "GET",
                          "/api/student/dashboard").body["invoices"]})
        self.assertEqual(self.request(outsider, "POST", "/api/student/payment-claims", {
            "invoice_id": invoice_id, "slip_mime": "image/png", "slip_base64": "invalid",
        }).status, 404)
        payment = self.request(self.admin, "POST", "/api/admin/payments", {
            "invoice_id": invoice_id, "amount_satang": course["price_satang"],
            "method": "bank_transfer", "reference": "QA-TRANSFER-ONE",
        })
        self.assertEqual(payment.status, 201, payment.body)
        receipt_path = f"/api/receipts/{payment.body['receipt_id']}"
        self.assertEqual(self.request(self.origin, "GET", receipt_path).status, 401)
        self.assertEqual(self.request(outsider, "GET", receipt_path).status, 404)
        self.assertEqual(self.request(self.student, "GET", receipt_path).status, 200)
        self.assertEqual(self.request(self.admin, "GET", receipt_path).status, 200)
        without_csrf = {key: value for key, value in self.student.items()
                        if key != "x-csrf-token"}
        self.assertEqual(self.request(without_csrf, "POST", "/api/student/enrollments", {
            "course_id": course["id"]}).status, 403)

    def test_partially_booked_slot_shows_remaining_time_to_student_and_teacher(self) -> None:
        teacher = self.request(self.admin, "POST", "/api/admin/staff", {
            "name": "QA Teacher", "email": "qa.teacher@example.com",
        })
        self.assertEqual(teacher.status, 201, teacher.body)
        teacher_id = teacher.body["user"]["id"]
        self.assertEqual(self.request(self.admin, "POST",
                                      f"/api/admin/users/{teacher_id}/reset-password", {}).status, 200)
        # An admin-created teacher starts with a temporary password. Activate
        # it in this synthetic fixture to read the teacher's own availability.
        with self.app.connect() as conn:
            from backend import hash_password
            conn.execute("UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?",
                         (hash_password(PASSWORD), teacher_id))
        teacher_auth = self.login("qa.teacher@example.com")
        course = self.request(self.admin, "POST", "/api/admin/courses", {
            "subject": "physics", "level": "high", "course_format": "individual",
            "teacher_id": teacher_id, "capacity": 1, "price_satang": 30_000,
        })
        self.assertEqual(course.status, 201, course.body)
        enrolled = self.request(self.student, "POST", "/api/student/enrollments", {
            "course_id": course.body["course_id"], "grade": "M4", "school": "QA School",
            "guardian_name": "Guardian", "guardian_phone": "0812345678",
            "guardian_relationship": "mother", "confirm_purchase": True,
        })
        self.assertEqual(enrolled.status, 201, enrolled.body)
        start = (datetime.now(ZONE) + timedelta(days=2)).replace(
            hour=13, minute=0, second=0, microsecond=0)
        mid = start + timedelta(hours=1)
        end = start + timedelta(hours=2)
        slot = self.request(self.admin, "POST", "/api/admin/teacher-availability", {
            "teacher_id": teacher_id, "starts_at": start.isoformat(),
            "ends_at": end.isoformat(),
        })
        self.assertEqual(slot.status, 201, slot.body)
        first = self.request(self.admin, "POST", "/api/admin/classes", {
            "course_id": course.body["course_id"], "teacher_id": teacher_id,
            "room_name": "A", "starts_at": start.isoformat(),
            "ends_at": mid.isoformat(), "student_ids": [self.student_id],
        })
        self.assertEqual(first.status, 201, first.body)
        admin_slots = self.request(self.admin, "GET", "/api/teacher-availability").body["slots"]
        self.assertTrue(next(row for row in admin_slots if row["id"] == slot.body["slot_id"])["booked"])
        for role, actor in (("student", self.student), ("teacher", teacher_auth)):
            with self.subTest(role=role):
                free = self.request(actor, "GET", "/api/teacher-availability").body["slots"]
                remaining = [row for row in free if row["id"] == slot.body["slot_id"]]
                self.assertEqual(len(remaining), 1)
                self.assertEqual(remaining[0]["starts_at"], int(mid.timestamp()))
                self.assertEqual(remaining[0]["ends_at"], int(end.timestamp()))
                self.assertFalse(remaining[0]["booked"])
        second = self.request(self.admin, "POST", "/api/admin/classes", {
            "course_id": course.body["course_id"], "teacher_id": teacher_id,
            "room_name": "B", "starts_at": mid.isoformat(),
            "ends_at": end.isoformat(), "student_ids": [self.student_id],
        })
        self.assertEqual(second.status, 201, second.body)
        self.assertFalse([row for row in self.request(self.student, "GET",
                          "/api/teacher-availability").body["slots"]
                          if row["id"] == slot.body["slot_id"]])
        cancelled = self.request(self.admin, "POST",
                                 f"/api/admin/classes/{second.body['session_id']}/cancel", {})
        self.assertEqual(cancelled.status, 200, cancelled.body)
        restored = [row for row in self.request(self.student, "GET",
                    "/api/teacher-availability").body["slots"]
                    if row["id"] == slot.body["slot_id"]]
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["starts_at"], int(mid.timestamp()))


if __name__ == "__main__":
    unittest.main()
