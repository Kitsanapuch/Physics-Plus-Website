"""Payment claims produce private, actionable alerts for the administrator."""

from __future__ import annotations

import tempfile
import unittest
import base64
from pathlib import Path

from backend import PhysicsPlusApp


TEST_SLIP = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)).decode("ascii")


class PaymentAlertTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.owner = PhysicsPlusApp(Path(temp.name) / "site.sqlite3", "https://school.example")
        self.origin = {"host": "school.example", "origin": "https://school.example"}
        self.owner.bootstrap_admin(None, "Administrator", "SyntheticPassword123!", "physicsplus")
        admin_login = self.call("POST", "/api/auth/login", {
            "login": "physicsplus", "password": "SyntheticPassword123!",
        })
        self.assertEqual(admin_login.status, 200, admin_login.body)
        self.admin_cookie = admin_login.headers["Set-Cookie"].split(";", 1)[0]
        self.admin_csrf = admin_login.body["csrf_token"]
        registration = self.call("POST", "/api/auth/register", {
            "name": "Example Student", "email": "student@example.com",
            "password": "SyntheticPassword123!", "grade": "M4", "school": "Example School",
            "guardian_name": "Guardian", "guardian_phone": "0812345678",
            "guardian_relationship": "mother",
        })
        self.assertEqual(registration.status, 201, registration.body)
        self.student_cookie = registration.headers["Set-Cookie"].split(";", 1)[0]
        self.student_csrf = registration.body["csrf_token"]

    def call(self, method: str, path: str, data: dict | None = None,
             *, cookie: str = "", csrf: str = ""):
        headers = dict(self.origin)
        if cookie:
            headers["cookie"] = cookie
        if csrf:
            headers["x-csrf-token"] = csrf
        return self.owner.handle(method, path, data, headers, "127.0.0.1")

    def test_student_claim_alerts_admin_until_reviewed(self) -> None:
        alert_path = "/api/admin/payment-alerts"
        self.assertEqual(self.call("GET", alert_path).status, 401)
        self.assertEqual(self.call("GET", alert_path, cookie=self.student_cookie).status, 403)
        empty = self.call("GET", alert_path, cookie=self.admin_cookie)
        self.assertEqual(empty.body, {"pending_payment_claims": [], "pending_count": 0})

        course = next(course for course in
                      self.call("GET", "/api/courses", cookie=self.student_cookie).body["courses"]
                      if course["level"] == "high")
        enrollment = self.call("POST", "/api/student/enrollments", {
            "course_id": course["id"], "grade": "M4", "school": "Example School",
            "guardian_name": "Guardian", "guardian_phone": "0812345678",
            "guardian_relationship": "mother",
        }, cookie=self.student_cookie, csrf=self.student_csrf)
        self.assertEqual(enrollment.status, 201, enrollment.body)
        invoice = self.call("POST", "/api/admin/invoices", {
            "enrollment_id": enrollment.body["enrollment_id"],
            "amount_satang": 450_000, "due_date": "2030-01-15",
        }, cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(invoice.status, 201, invoice.body)
        claim = self.call("POST", "/api/student/payment-claims", {
            "invoice_id": invoice.body["invoice_id"],
            "slip_mime": "image/png", "slip_base64": TEST_SLIP,
        }, cookie=self.student_cookie, csrf=self.student_csrf)
        self.assertEqual(claim.status, 201, claim.body)
        slip_path = f"/api/admin/payment-claims/{claim.body['claim_id']}/slip"
        self.assertEqual(self.call("GET", slip_path).status, 401)
        self.assertEqual(self.call("GET", slip_path, cookie=self.student_cookie).status, 403)
        self.assertEqual(self.call("GET", slip_path, cookie=self.admin_cookie).body["slip_base64"], TEST_SLIP)

        alerts = self.call("GET", alert_path, cookie=self.admin_cookie)
        self.assertEqual(alerts.status, 200, alerts.body)
        self.assertEqual(alerts.body["pending_count"], 1)
        self.assertEqual(alerts.body["pending_payment_claims"][0]["id"], claim.body["claim_id"])
        self.assertEqual(alerts.body["pending_payment_claims"][0]["student_name"], "Example Student")
        self.assertEqual(alerts.body["pending_payment_claims"][0]["amount_satang"], 450_000)
        self.assertNotIn("guardian_phone", str(alerts.body))
        direct_payment = self.call("POST", "/api/admin/payments", {
            "invoice_id": invoice.body["invoice_id"], "amount_satang": 450_000,
            "method": "bank_transfer", "reference": "DIRECT-TRANSFER-1",
        }, cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(direct_payment.status, 409, direct_payment.body)
        self.assertEqual(direct_payment.body["error"]["code"], "claim_pending")
        with self.owner.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM payments").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT status FROM invoices WHERE id=?",
                                          (invoice.body["invoice_id"],)).fetchone()[0], "unpaid")
        with self.owner.connect() as conn:
            notices = conn.execute(
                "SELECT COUNT(*) FROM notification_outbox WHERE event_type='payment_claim_submitted' "
                "AND recipient_kind='admin'"
            ).fetchone()[0]
        self.assertEqual(notices, 1)

        review = self.call("POST", f"/api/admin/payment-claims/{claim.body['claim_id']}/approve", {},
                           cookie=self.admin_cookie, csrf=self.admin_csrf)
        self.assertEqual(review.status, 200, review.body)
        self.assertEqual(self.call("GET", alert_path, cookie=self.admin_cookie).body,
                         {"pending_payment_claims": [], "pending_count": 0})


if __name__ == "__main__":
    unittest.main()
