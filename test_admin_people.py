"""Private admin directory and bounded person history regression checks."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import PhysicsPlusApp


PASSWORD = "SyntheticAdminPeoplePassword123!"
ZONE = timezone(timedelta(hours=7))


class AdminPeopleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = PhysicsPlusApp(Path(temporary.name) / "people.sqlite3", "https://school.example")
        self.origin = {"host": "school.example", "origin": "https://school.example"}
        self.app.bootstrap_admin(None, "Directory Admin", PASSWORD, "directoryadmin")
        self.admin = self.login("directoryadmin")
        registered = self.request(self.origin, "POST", "/api/auth/register", {
            "name": "History Student", "email": "history.student@example.com", "password": PASSWORD,
            "grade": "M4", "school": "History School", "guardian_name": "Parent Test",
            "guardian_phone": "0812345678", "guardian_relationship": "mother",
        })
        self.assertEqual(registered.status, 201, registered.body)
        self.student_id = registered.body["user"]["id"]
        self.student_code = registered.body["user"]["student_code"]
        self.student = self.auth_from_response(registered)
        staff = self.request(self.admin, "POST", "/api/admin/staff", {"name": "History Teacher"})
        self.assertEqual(staff.status, 201, staff.body)
        self.teacher_id = staff.body["user"]["id"]

    def request(self, auth: dict[str, str], method: str, path: str,
                data: dict | None = None):
        return self.app.handle(method, path, data, auth, "127.0.0.1")

    def auth_from_response(self, response) -> dict[str, str]:
        return {**self.origin,
                "cookie": response.headers["Set-Cookie"].split(";", 1)[0],
                "x-csrf-token": response.body["csrf_token"]}

    def login(self, username: str) -> dict[str, str]:
        response = self.request(self.origin, "POST", "/api/auth/login",
                                {"login": username, "password": PASSWORD})
        self.assertEqual(response.status, 200, response.body)
        return self.auth_from_response(response)

    def test_directory_is_bounded_private_and_matches_literal_search(self) -> None:
        url = f"/api/admin/people/search?q={self.student_code}&role=student"
        self.assertEqual(self.request(self.origin, "GET", url).status, 401)
        self.assertEqual(self.request(self.student, "GET", url).status, 403)
        result = self.request(self.admin, "GET", url)
        self.assertEqual(result.status, 200, result.body)
        self.assertEqual([item["id"] for item in result.body["people"]], [self.student_id])
        self.assertFalse(result.body["has_more"])
        self.assertNotIn("guardian_phone", result.body["people"][0])
        self.assertNotIn("password_hash", result.body["people"][0])
        self.assertEqual(self.request(self.admin, "GET", "/api/admin/people/search?q=%25").body["people"], [])
        self.assertEqual(self.request(self.admin, "GET", "/api/admin/people/search?role=admin").status, 400)
        self.assertEqual(self.request(self.admin, "GET", "/api/admin/people/search?q=" + "x" * 81).status, 400)

    def test_pending_teacher_identity_never_exposes_internal_placeholder_email(self) -> None:
        searched = self.request(self.admin, "GET", "/api/admin/people/search?q=History%20Teacher&role=teacher")
        self.assertEqual(searched.status, 200, searched.body)
        teacher = next(item for item in searched.body["people"] if item["id"] == self.teacher_id)
        self.assertIsNone(teacher["email"])
        self.assertTrue(teacher["roster_pending"])
        dossier = self.request(self.admin, "GET", f"/api/admin/people/{self.teacher_id}/history")
        self.assertEqual(dossier.status, 200, dossier.body)
        self.assertIsNone(dossier.body["person"]["email"])
        self.assertTrue(dossier.body["person"]["roster_pending"])
        self.assertNotIn("password_hash", dossier.body["person"])

    def test_student_dossier_contains_tuition_and_classes_without_slip_bytes(self) -> None:
        teacher_course = self.request(self.admin, "POST", "/api/admin/courses", {
            "subject": "physics", "level": "high", "course_format": "group",
            "teacher_id": self.teacher_id, "capacity": 10, "price_satang": 450_000,
        })
        self.assertEqual(teacher_course.status, 201, teacher_course.body)
        course_id = teacher_course.body["course_id"]
        purchase = self.request(self.student, "POST", "/api/student/enrollments", {
            "course_id": course_id, "grade": "M4", "school": "History School",
            "guardian_name": "Parent Test", "guardian_phone": "0812345678",
            "guardian_relationship": "mother", "confirm_purchase": True,
        })
        self.assertEqual(purchase.status, 201, purchase.body)
        paid = self.request(self.admin, "POST", "/api/admin/payments", {
            "invoice_id": purchase.body["invoice_id"], "amount_satang": 450_000,
            "method": "bank_transfer", "reference": "DIRECTORY-TEST-PAYMENT",
        })
        self.assertEqual(paid.status, 201, paid.body)
        start = (datetime.now(ZONE) + timedelta(days=2)).replace(
            hour=13, minute=0, second=0, microsecond=0)
        scheduled = self.request(self.admin, "POST", "/api/admin/classes", {
            "course_id": course_id, "teacher_id": self.teacher_id,
            "room_name": "ห้องใหญ่", "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
            "student_ids": [self.student_id],
        })
        self.assertEqual(scheduled.status, 201, scheduled.body)

        path = f"/api/admin/people/{self.student_id}/history"
        self.assertEqual(self.request(self.student, "GET", path).status, 403)
        dossier = self.request(self.admin, "GET", path)
        self.assertEqual(dossier.status, 200, dossier.body)
        body = dossier.body
        self.assertEqual(body["person"]["guardian_phone"], "0812345678")
        self.assertEqual(body["summary"]["active_enrollments"], 1)
        self.assertEqual(body["summary"]["billed_satang"], 450_000)
        self.assertEqual(body["summary"]["paid_satang"], 450_000)
        self.assertEqual(body["summary"]["outstanding_satang"], 0)
        self.assertEqual(body["summary"]["classes_total"], 1)
        self.assertEqual(body["payments"][0]["receipt_id"], paid.body["receipt_id"])
        self.assertEqual(body["classes"][0]["room_name"], "ห้องใหญ่")
        self.assertTrue(any(item["entity"] == "payment" for item in body["timeline"]))
        self.assertNotIn("slip_base64", str(body))
        self.assertNotIn("password_hash", str(body))

        overview = self.request(self.admin, "GET", "/api/admin/overview")
        self.assertEqual(overview.status, 200, overview.body)
        self.assertGreaterEqual(overview.body["student_count"], 1)
        self.assertGreaterEqual(overview.body["teacher_count"], 1)
        self.assertGreaterEqual(overview.body["active_course_count"], 1)
        self.assertEqual(overview.body["active_enrollment_count"], 1)
        self.assertEqual(overview.body["upcoming_class_count"], 1)
        self.assertEqual(overview.body["paid_this_month_satang"], 450_000)

        # A removed roster member must retain the schedule change in their
        # audit history even though they no longer appear in the current class.
        removed = self.request(self.admin, "PUT", f"/api/admin/classes/{scheduled.body['session_id']}", {
            "course_id": course_id, "teacher_id": self.teacher_id,
            "room_name": "ห้องใหญ่", "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(), "student_ids": [],
        })
        self.assertEqual(removed.status, 200, removed.body)
        after_removal = self.request(self.admin, "GET", path)
        self.assertEqual(after_removal.status, 200, after_removal.body)
        self.assertEqual(after_removal.body["summary"]["classes_total"], 0)
        self.assertTrue(any(event["entity"] == "class_session"
                            and event["action"] == "notify_update"
                            for event in after_removal.body["timeline"]))

    def test_timeline_cursor_preserves_all_events_without_duplicates(self) -> None:
        with self.app.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for number in range(46):
                self.app.audit(conn, self.student_id, "user", self.student_id,
                               f"history_event_{number}")
            conn.commit()
        path = f"/api/admin/people/{self.student_id}/history"
        first = self.request(self.admin, "GET", path)
        self.assertEqual(first.status, 200, first.body)
        self.assertEqual(len(first.body["timeline"]), 40)
        self.assertIsNotNone(first.body["next_cursor"])
        second = self.request(self.admin, "GET", path + "?cursor=" + first.body["next_cursor"])
        self.assertEqual(second.status, 200, second.body)
        ids = [item["id"] for item in first.body["timeline"] + second.body["timeline"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 46)
        self.assertEqual(self.request(self.admin, "GET", path + "?cursor=wrong").status, 400)


if __name__ == "__main__":
    unittest.main()
