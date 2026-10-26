"""End-to-end local checks for the order, slip, notice and private-class flow."""

from __future__ import annotations

import base64
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend import PhysicsPlusApp


SLIP = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)).decode("ascii")


class PurchaseJourneyTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.owner = PhysicsPlusApp(Path(temp.name) / "site.sqlite3", "https://school.example")
        self.origin = {"host": "school.example", "origin": "https://school.example"}
        self.owner.bootstrap_admin(None, "Administrator", "SyntheticPassword123!", "physicsplus")
        login = self.call("POST", "/api/auth/login", {
            "login": "physicsplus", "password": "SyntheticPassword123!",
        })
        self.admin_cookie = login.headers["Set-Cookie"].split(";", 1)[0]
        self.admin_csrf = login.body["csrf_token"]
        register = self.call("POST", "/api/auth/register", {
            "name": "Student One", "email": "one@example.com",
            "password": "SyntheticPassword123!", "grade": "M4",
            "school": "Example School", "guardian_name": "Guardian One",
            "guardian_phone": "0812345678", "guardian_relationship": "mother",
        })
        self.assertEqual(register.status, 201, register.body)
        self.student_id = register.body["user"]["id"]
        self.student_cookie = register.headers["Set-Cookie"].split(";", 1)[0]
        self.student_csrf = register.body["csrf_token"]

    def call(self, method: str, path: str, data: dict | None = None,
             *, cookie: str = "", csrf: str = ""):
        headers = dict(self.origin)
        if cookie:
            headers["cookie"] = cookie
        if csrf:
            headers["x-csrf-token"] = csrf
        return self.owner.handle(method, path, data, headers, "127.0.0.1")

    def test_purchase_creates_server_priced_invoice_and_personal_receipt(self) -> None:
        course = next(item for item in self.call("GET", "/api/courses", cookie=self.student_cookie).body["courses"]
                      if item["level"] == "high")
        enroll = self.call("POST", "/api/student/enrollments", {
            "course_id": course["id"], "grade": "M4", "school": "Example School",
            "guardian_name": "Guardian One", "guardian_phone": "0812345678",
            "guardian_relationship": "mother", "confirm_purchase": True,
        }, cookie=self.student_cookie, csrf=self.student_csrf)
        self.assertEqual(enroll.status, 201, enroll.body)
        self.assertEqual(enroll.body["amount_satang"], course["price_satang"])
        invoice = next(item for item in self.call("GET", "/api/student/dashboard",
                           cookie=self.student_cookie).body["invoices"]
                       if item["id"] == enroll.body["invoice_id"])
        self.assertEqual(invoice["amount_satang"], course["price_satang"])
        self.assertEqual(self.call("GET", "/api/admin/payment-alerts",
                                  cookie=self.student_cookie).status, 403)
        claim = self.call("POST", "/api/student/payment-claims", {
            "invoice_id": invoice["id"], "slip_mime": "image/png", "slip_base64": SLIP,
        }, cookie=self.student_cookie, csrf=self.student_csrf)
        self.assertEqual(claim.status, 201, claim.body)
        alerts = self.call("GET", "/api/admin/payment-alerts", cookie=self.admin_cookie)
        self.assertEqual(alerts.body["pending_count"], 1)
        self.assertEqual(alerts.body["pending_payment_claims"][0]["student_name"], "Student One")
        self.assertEqual(alerts.body["pending_payment_claims"][0]["invoice_number"], invoice["invoice_number"])
        verification = self.call("POST", f"/api/admin/payment-claims/{claim.body['claim_id']}/verification",
                                 {"verdict": "correct", "bank_confirmed": True},
                                 cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(verification.status, 200, verification.body)
        approve = self.call("POST", f"/api/admin/payment-claims/{claim.body['claim_id']}/approve",
                            {}, cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(approve.status, 200, approve.body)
        dashboard = self.call("GET", "/api/student/dashboard", cookie=self.student_cookie).body
        self.assertEqual(next(item for item in dashboard["invoices"] if item["id"] == invoice["id"])["status"], "paid")
        receipt = next(item for item in dashboard["receipts"] if item["invoice_id"] == invoice["id"])
        private_receipt = self.call("GET", f"/api/receipts/{receipt['id']}",
                                    cookie=self.student_cookie).body["receipt"]
        self.assertEqual(private_receipt["student_id"], self.student_id)
        self.assertEqual(receipt["course_title"], course["title"])
        inbox = self.call("GET", "/api/notifications", cookie=self.student_cookie).body
        self.assertIn("payment_claim_approved", [item["event_type"] for item in inbox["notifications"]])
        self.assertNotIn("payment_claim_submitted", [item["event_type"] for item in inbox["notifications"]])

    def test_individual_class_requires_admin_slot_and_one_student(self) -> None:
        teacher = self.call("POST", "/api/admin/staff", {
            "name": "Teacher One", "email": "teacher@example.com", "role": "teacher",
        }, cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(teacher.status, 201, teacher.body)
        teacher_id = teacher.body["user"]["id"]
        course = self.call("POST", "/api/admin/courses", {
            "subject": "physics", "level": "high", "course_format": "individual",
            "target_sessions": 8, "teacher_id": teacher_id, "capacity": 1,
            "price_satang": 30_000,
        }, cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(course.status, 201, course.body)
        student = self.call("POST", "/api/student/enrollments", {
            "course_id": course.body["course_id"], "grade": "M4",
            "school": "Example School", "guardian_name": "Guardian One",
            "guardian_phone": "0812345678", "guardian_relationship": "mother",
            "confirm_purchase": True,
        }, cookie=self.student_cookie, csrf=self.student_csrf)
        self.assertEqual(student.status, 201, student.body)
        start = datetime.now(timezone(timedelta(hours=7))) + timedelta(days=2)
        end = start + timedelta(hours=1)
        session = {"course_id": course.body["course_id"], "teacher_id": teacher_id,
                   "room_name": "ห้องเรียนเดี่ยว A", "starts_at": start.isoformat(),
                   "ends_at": end.isoformat(), "student_ids": [self.student_id]}
        blocked = self.call("POST", "/api/admin/classes", session,
                            cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(blocked.status, 409, blocked.body)
        self.assertEqual(blocked.body["error"]["code"], "teacher_unavailable")
        slot = self.call("POST", "/api/admin/teacher-availability", {
            "teacher_id": teacher_id, "starts_at": start.isoformat(), "ends_at": end.isoformat(),
        }, cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(slot.status, 201, slot.body)
        created = self.call("POST", "/api/admin/classes", session,
                            cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(created.status, 201, created.body)
        classes = self.call("GET", "/api/student/classes", cookie=self.student_cookie).body["classes"]
        self.assertTrue(any(item["id"] == created.body["session_id"] for item in classes))


if __name__ == "__main__":
    unittest.main()
