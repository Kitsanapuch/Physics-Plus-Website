"""Independent accounting checks for taught-hour individual tuition.

Only synthetic users and a temporary SQLite database are used.  The tests
exercise public API boundaries as well as invoice and receipt persistence.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from backend import PhysicsPlusApp, hash_password, now, uid


BANGKOK = ZoneInfo("Asia/Bangkok")
PASSWORD = "SyntheticBillingPassword123!"
RATE_SATANG_PER_HOUR = 30_000


class IndividualMonthlyBillingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.site = PhysicsPlusApp(Path(directory.name) / "school.sqlite3", "https://school.example")
        self.headers = {"host": "school.example", "origin": "https://school.example"}
        self.site.bootstrap_admin(None, "Test Admin", PASSWORD, "billingadmin")
        self.admin = self._login("billingadmin")
        self.student = self._register_student("first@example.com", "First Student")
        self.other_student = self._register_student("second@example.com", "Second Student")
        self.teacher_id = uid()
        with self.site.connect() as conn:
            conn.execute(
                "INSERT INTO users(id,email,name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                (self.teacher_id, "teacher@example.com", "Test Teacher", hash_password(PASSWORD),
                 "teacher", now()),
            )
        self.teacher = self._login("teacher@example.com")
        course = self._request("admin", "POST", "/api/admin/courses", {
            "subject": "physics", "level": "high", "course_format": "individual",
            "teacher_id": self.teacher_id, "capacity": 1, "price_satang": RATE_SATANG_PER_HOUR,
            "target_sessions": 12,
        })
        self.assertEqual(course.status, 201, course.body)
        self.course_id = course.body["course_id"]
        self.current_month = datetime.now(BANGKOK).replace(day=1, hour=0, minute=0, second=0,
                                                           microsecond=0)
        self.previous_month = (self.current_month - timedelta(days=1)).replace(day=1)
        self.period = self.previous_month.strftime("%Y-%m")

    def _login(self, login: str) -> dict[str, str]:
        result = self.site.handle("POST", "/api/auth/login", {"login": login, "password": PASSWORD},
                                  self.headers, "127.0.0.1")
        self.assertEqual(result.status, 200, result.body)
        return {**self.headers, "cookie": result.headers["Set-Cookie"].split(";", 1)[0],
                "x-csrf-token": result.body["csrf_token"]}

    def _register_student(self, email: str, name: str) -> dict[str, str]:
        result = self.site.handle("POST", "/api/auth/register", {
            "name": name, "email": email, "password": PASSWORD, "grade": "M4",
            "school": "Test School", "guardian_name": "Test Parent",
            "guardian_phone": "0812345678", "guardian_relationship": "mother",
        }, self.headers, "127.0.0.1")
        self.assertEqual(result.status, 201, result.body)
        return {**self.headers, "id": result.body["user"]["id"],
                "cookie": result.headers["Set-Cookie"].split(";", 1)[0],
                "x-csrf-token": result.body["csrf_token"]}

    def _request(self, actor: str, method: str, path: str, data: dict | None = None):
        headers = getattr(self, actor)
        return self.site.handle(method, path, data, headers, "127.0.0.1")

    def _enroll(self):
        return self._request("student", "POST", "/api/student/enrollments", {
            "course_id": self.course_id, "grade": "M4", "school": "Test School",
            "guardian_name": "Test Parent", "guardian_phone": "0812345678",
            "guardian_relationship": "mother", "confirm_purchase": True,
        })

    def _historical_class(self, *, day: int, attended: bool, payable_minutes: int | None,
                          break_minutes: int = 0) -> tuple[str, str | None]:
        """Seed a past, administratively scheduled class with realistic roster and time."""
        date = self.previous_month.replace(day=day, hour=13)
        start = int(date.timestamp())
        session_id = uid()
        entry_id = uid() if payable_minutes is not None else None
        with self.site.connect() as conn:
            conn.execute(
                "INSERT INTO class_sessions(id,course_id,room_name,teacher_id,starts_at,ends_at,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (session_id, self.course_id, "Test Room", self.teacher_id, start,
                 start + 2 * 3600, now()),
            )
            conn.execute("INSERT INTO session_students(session_id,student_id) VALUES(?,?)",
                         (session_id, self.student["id"]))
            conn.execute(
                "INSERT INTO attendance(session_id,student_id,status,recorded_by,recorded_at) "
                "VALUES(?,?,?,?,?)",
                (session_id, self.student["id"], "present" if attended else "absent",
                 self.admin_user_id(conn), now()),
            )
            if entry_id:
                # The administrator later approves this submitted entry via the API.
                actual_end = start + (payable_minutes + break_minutes) * 60
                conn.execute(
                    "INSERT INTO time_entries(id,session_id,teacher_id,actual_start,actual_end,"
                    "break_minutes,submitted_at) VALUES(?,?,?,?,?,?,?)",
                    (entry_id, session_id, self.teacher_id, start, actual_end, break_minutes, now()),
                )
        return session_id, entry_id

    @staticmethod
    def admin_user_id(conn) -> str:
        return conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]

    def test_enrollment_waits_for_taught_hours_and_blocks_arbitrary_invoice(self) -> None:
        bad_rate = self._request("admin", "PUT", f"/api/admin/courses/{self.course_id}", {
            "price_satang": 650_000,
        })
        self.assertIn(bad_rate.status, {400, 409}, bad_rate.body)
        catalog = self._request("student", "GET", "/api/courses").body["courses"]
        self.assertEqual(next(c for c in catalog if c["id"] == self.course_id)["price_satang"],
                         RATE_SATANG_PER_HOUR)
        spoofed = self._request("student", "POST", "/api/student/enrollments", {
            "course_id": self.course_id, "grade": "M4", "school": "Test School",
            "guardian_name": "Test Parent", "guardian_phone": "0812345678",
            "guardian_relationship": "mother", "confirm_purchase": True,
            "amount_satang": 1,
        })
        self.assertEqual(spoofed.status, 400, spoofed.body)
        enrollment = self._enroll()
        self.assertEqual(enrollment.status, 201, enrollment.body)
        self.assertEqual(enrollment.body["status"], "awaiting_monthly_billing")
        self.assertNotIn("invoice_id", enrollment.body)
        dashboard = self._request("student", "GET", "/api/student/dashboard")
        self.assertEqual(dashboard.body["invoices"], [])
        arbitrary = self._request("admin", "POST", "/api/admin/invoices", {
            "enrollment_id": enrollment.body["enrollment_id"], "amount_satang": 100,
            "due_date": self.current_month.date().isoformat(),
        })
        self.assertEqual(arbitrary.status, 409, arbitrary.body)
        self.assertEqual(self._request("student", "GET", "/api/student/dashboard").body["invoices"], [])

    def test_month_close_uses_approved_actual_minutes_not_teacher_wage(self) -> None:
        enrollment = self._enroll()
        self.assertEqual(enrollment.status, 201, enrollment.body)
        # Existing 1:1 catalog rows could carry a former fixed-course price.
        # Billing must derive its amount from the new hourly rule, never that
        # stale catalog value or the teacher's independently approved wage.
        with self.site.connect() as conn:
            conn.execute("UPDATE courses SET price_satang=650000 WHERE id=?", (self.course_id,))
        session_id, pending_id = self._historical_class(day=10, attended=True, payable_minutes=105,
                                                        break_minutes=15)
        self.assertIsNotNone(pending_id)
        preview_path = f"/api/admin/individual-billing?period={self.period}"
        preview = self._request("admin", "GET", preview_path)
        self.assertEqual(preview.status, 200, preview.body)
        self.assertFalse(preview.body["ready"])
        self.assertTrue(preview.body["blockers"])
        premature = self._request("admin", "POST", "/api/admin/individual-billing",
                                  {"period": self.period})
        self.assertEqual(premature.status, 409, premature.body)
        self.assertEqual(self._request("student", "GET", "/api/student/dashboard").body["invoices"], [])

        # An exceptional teacher wage of 9,999 THB must not change the pupil's
        # immutable 300 THB/hour tuition rate.
        approved = self._request("admin", "POST",
                                 f"/api/admin/time-entries/{pending_id}/approve-override", {
                                     "amount_satang": 999_900, "reason": "Synthetic wage exception",
                                     "source_reference": "TEST-WAGE-EXCEPTION",
                                 })
        self.assertEqual(approved.status, 200, approved.body)
        self.assertEqual(approved.body["payable_minutes"], 105)
        student_classes = self._request("student", "GET", "/api/student/classes")
        self.assertEqual(student_classes.status, 200, student_classes.body)
        taught = next(item for item in student_classes.body["classes"] if item["id"] == session_id)
        self.assertEqual(taught["time_status"], "approved")
        self.assertEqual(taught["student_attendance"], "present")
        self.assertFalse(self._request("other_student", "GET", "/api/student/classes").body["classes"])
        self._historical_class(day=11, attended=False, payable_minutes=60)
        # Absence has no student tuition, even if teacher time is approved.
        with self.site.connect() as conn:
            conn.execute(
                "UPDATE time_entries SET status='approved',payable_minutes=60,student_count=0,"
                "amount_satang=30000,pricing_type='manual_amount' "
                "WHERE session_id IN (SELECT id FROM class_sessions WHERE starts_at=?)",
                (int(self.previous_month.replace(day=11, hour=13).timestamp()),),
            )
        preview = self._request("admin", "GET", preview_path)
        self.assertEqual(preview.status, 200, preview.body)
        self.assertTrue(preview.body["ready"], preview.body)
        self.assertEqual(preview.body["rate_satang_per_hour"], RATE_SATANG_PER_HOUR)
        item = next(row for row in preview.body["items"]
                    if row["enrollment_id"] == enrollment.body["enrollment_id"])
        self.assertEqual(item["minutes"], 105)
        self.assertEqual(item["amount_satang"], 52_500)
        self.assertEqual(item["session_count"], 1)
        self.assertEqual(preview.body["total_satang"], 52_500)

        with self.site.connect() as conn:
            sheet_revision_before = conn.execute(
                "SELECT revision FROM sheet_sync_state WHERE dataset='invoices'"
            ).fetchone()[0]
        issued = self._request("admin", "POST", "/api/admin/individual-billing",
                               {"period": self.period})
        self.assertEqual(issued.status, 200, issued.body)
        self.assertEqual(len(issued.body["issued"]), 1)
        invoice_id = issued.body["issued"][0]["invoice_id"]
        self.assertEqual(issued.body["total_satang"], 52_500)
        with self.site.connect() as conn:
            sheet_revision_after = conn.execute(
                "SELECT revision FROM sheet_sync_state WHERE dataset='invoices'"
            ).fetchone()[0]
            notice = conn.execute(
                "SELECT n.recipient_user_id,n.event_type,a.entity,a.entity_id "
                "FROM inbox_notifications n JOIN audit_events a ON a.id=n.audit_event_id "
                "WHERE a.entity='invoice' AND a.entity_id=? AND n.event_type='invoice_created'",
                (invoice_id,),
            ).fetchone()
        self.assertGreater(sheet_revision_after, sheet_revision_before)
        self.assertIsNotNone(notice)
        self.assertEqual(notice["recipient_user_id"], self.student["id"])
        duplicate = self._request("admin", "POST", "/api/admin/individual-billing",
                                  {"period": self.period})
        self.assertEqual(duplicate.status, 200, duplicate.body)
        self.assertEqual(duplicate.body["issued"], [])
        with self.site.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoices WHERE id=?", (invoice_id,)).fetchone()[0], 1)
        own = self._request("student", "GET", "/api/student/dashboard")
        other = self._request("other_student", "GET", "/api/student/dashboard")
        self.assertEqual(next(row for row in own.body["invoices"] if row["id"] == invoice_id)["amount_satang"],
                         52_500)
        self.assertFalse(any(row["id"] == invoice_id for row in other.body["invoices"]))
        self.assertEqual(self._request("student", "GET", preview_path).status, 403)
        self.assertEqual(self._request("teacher", "GET", preview_path).status, 403)

        payment = self._request("admin", "POST", "/api/admin/payments", {
            "invoice_id": invoice_id, "amount_satang": 52_500,
            "method": "bank_transfer", "reference": "SYNTHETIC-HOURLY-001",
        })
        self.assertEqual(payment.status, 201, payment.body)
        receipt = self._request("student", "GET", f"/api/receipts/{payment.body['receipt_id']}")
        self.assertEqual(receipt.status, 200, receipt.body)
        self.assertEqual(receipt.body["receipt"]["student_id"], self.student["id"])
        self.assertEqual(receipt.body["receipt"]["amount_satang"], 52_500)
        self.assertEqual(self._request("other_student", "GET",
                                        f"/api/receipts/{payment.body['receipt_id']}").status, 404)

        # A free slot can survive from before the close. It must not permit
        # creating a backdated, unbillable lesson after this month has one
        # final invoice per enrollment.
        backdated = self.previous_month.replace(day=12, hour=13)
        start = int(backdated.timestamp())
        with self.site.connect() as conn:
            conn.execute(
                "INSERT INTO teacher_availability(id,teacher_id,starts_at,ends_at,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (uid(), self.teacher_id, start, start + 3600, self.admin_user_id(conn), now()),
            )
        late_class = self._request("admin", "POST", "/api/admin/classes", {
            "course_id": self.course_id, "teacher_id": self.teacher_id,
            "room_name": "Late Test Room", "starts_at": backdated.isoformat(),
            "ends_at": (backdated + timedelta(hours=1)).isoformat(),
            "student_ids": [self.student["id"]],
        })
        self.assertEqual(late_class.status, 409, late_class.body)

        # Session and actual teaching can straddle midnight at month change.
        # The teacher cannot shift a new entry into a month already billed.
        scheduled = self.current_month.replace(minute=15)
        crossover_id = uid()
        with self.site.connect() as conn:
            conn.execute(
                "INSERT INTO class_sessions(id,course_id,room_name,teacher_id,starts_at,ends_at,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (crossover_id, self.course_id, "Midnight Room", self.teacher_id,
                 int(scheduled.timestamp()), int((scheduled + timedelta(hours=1)).timestamp()), now()),
            )
            conn.execute("INSERT INTO session_students(session_id,student_id) VALUES(?,?)",
                         (crossover_id, self.student["id"]))
        actual_start = scheduled - timedelta(minutes=30)
        crossover = self._request("teacher", "POST", "/api/teacher/time-entries", {
            "session_id": crossover_id, "actual_start": actual_start.isoformat(),
            "actual_end": (actual_start + timedelta(hours=1)).isoformat(),
            "break_minutes": 0,
        })
        self.assertEqual(crossover.status, 409, crossover.body)

    def test_legacy_upfront_invoice_requires_manual_review_before_monthly_bill(self) -> None:
        enrollment = self._enroll()
        self.assertEqual(enrollment.status, 201, enrollment.body)
        legacy_invoice_id = uid()
        with self.site.connect() as conn:
            conn.execute(
                "INSERT INTO invoices(id,invoice_number,enrollment_id,student_id,description,"
                "amount_satang,due_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (legacy_invoice_id, "PP-LEGACY-TEST", enrollment.body["enrollment_id"],
                 self.student["id"], "Earlier fixed-course tuition", 650_000,
                 self.previous_month.date().isoformat(), now()),
            )
        _, entry_id = self._historical_class(day=15, attended=True, payable_minutes=60)
        with self.site.connect() as conn:
            conn.execute(
                "UPDATE time_entries SET status='approved',payable_minutes=60,student_count=1,"
                "amount_satang=30000,pricing_type='manual_amount' WHERE id=?",
                (entry_id,),
            )
        preview = self._request("admin", "GET",
                                f"/api/admin/individual-billing?period={self.period}")
        self.assertEqual(preview.status, 200, preview.body)
        self.assertFalse(preview.body["ready"])
        self.assertIn("legacy_prepaid_invoice", {item["reason"] for item in preview.body["blockers"]})
        issued = self._request("admin", "POST", "/api/admin/individual-billing",
                               {"period": self.period})
        self.assertEqual(issued.status, 409, issued.body)
        with self.site.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM invoices WHERE student_id=?",
                                          (self.student["id"],)).fetchone()[0], 1)

    def test_current_and_future_month_cannot_be_closed(self) -> None:
        current_period = self.current_month.strftime("%Y-%m")
        future_period = (self.current_month + timedelta(days=40)).strftime("%Y-%m")
        for period in (current_period, future_period):
            with self.subTest(period=period):
                result = self._request("admin", "POST", "/api/admin/individual-billing",
                                       {"period": period})
                self.assertEqual(result.status, 409, result.body)
        malformed = self._request("admin", "GET", "/api/admin/individual-billing?period=2026-13")
        self.assertEqual(malformed.status, 400, malformed.body)
        without_csrf = self.site.handle(
            "POST", "/api/admin/individual-billing", {"period": self.period},
            {"host": "school.example", "origin": "https://school.example",
             "cookie": self.admin["cookie"]}, "127.0.0.1",
        )
        self.assertEqual(without_csrf.status, 403, without_csrf.body)

    def test_monthly_invoice_cannot_be_repriced_or_voided_and_payments_have_receipts(self) -> None:
        enrollment = self._enroll()
        self.assertEqual(enrollment.status, 201, enrollment.body)
        _, entry_id = self._historical_class(day=18, attended=True, payable_minutes=60)
        with self.site.connect() as conn:
            conn.execute(
                "UPDATE time_entries SET status='approved',payable_minutes=60,student_count=1,"
                "amount_satang=30000,pricing_type='manual_amount' WHERE id=?",
                (entry_id,),
            )
        closed = self._request("admin", "POST", "/api/admin/individual-billing",
                               {"period": self.period})
        self.assertEqual(closed.status, 200, closed.body)
        invoice_id = closed.body["issued"][0]["invoice_id"]
        for action in ("amount", "void", "restore"):
            with self.subTest(action=action):
                changed = self._request("admin", "POST",
                                        f"/api/admin/invoices/{invoice_id}/{action}",
                                        {"amount_satang": 100})
                self.assertEqual(changed.status, 409, changed.body)
        self.assertEqual(self._request("student", "POST", "/api/admin/individual-billing",
                                       {"period": self.period}).status, 403)
        self.assertEqual(self._request("teacher", "POST", "/api/admin/individual-billing",
                                       {"period": self.period}).status, 403)
        first = self._request("admin", "POST", "/api/admin/payments", {
            "invoice_id": invoice_id, "amount_satang": 10_000,
            "method": "bank_transfer", "reference": "SYNTHETIC-PART-1",
        })
        self.assertEqual(first.status, 201, first.body)
        repeated = self._request("admin", "POST", "/api/admin/payments", {
            "invoice_id": invoice_id, "amount_satang": 10_000,
            "method": "bank_transfer", "reference": "SYNTHETIC-PART-1",
        })
        self.assertEqual(repeated.status, 409, repeated.body)
        excess = self._request("admin", "POST", "/api/admin/payments", {
            "invoice_id": invoice_id, "amount_satang": 20_001,
            "method": "bank_transfer", "reference": "SYNTHETIC-TOO-MUCH",
        })
        self.assertEqual(excess.status, 409, excess.body)
        final = self._request("admin", "POST", "/api/admin/payments", {
            "invoice_id": invoice_id, "amount_satang": 20_000,
            "method": "bank_transfer", "reference": "SYNTHETIC-PART-2",
        })
        self.assertEqual(final.status, 201, final.body)
        with self.site.connect() as conn:
            invoice = conn.execute("SELECT amount_satang,status FROM invoices WHERE id=?",
                                   (invoice_id,)).fetchone()
            payments = conn.execute("SELECT COUNT(*),SUM(amount_satang) FROM payments WHERE invoice_id=?",
                                    (invoice_id,)).fetchone()
            receipts = conn.execute("SELECT COUNT(*),SUM(amount_satang) FROM receipts WHERE invoice_id=?",
                                    (invoice_id,)).fetchone()
            lines = conn.execute("SELECT SUM(minutes),SUM(amount_satang) FROM individual_billing_lines "
                                 "WHERE invoice_id=?", (invoice_id,)).fetchone()
        self.assertEqual(tuple(invoice), (30_000, "paid"))
        self.assertEqual(tuple(payments), (2, 30_000))
        self.assertEqual(tuple(receipts), (2, 30_000))
        self.assertEqual(tuple(lines), (60, 30_000))

    def test_month_close_rolls_back_invoice_and_lines_if_notification_fails(self) -> None:
        enrollment = self._enroll()
        self.assertEqual(enrollment.status, 201, enrollment.body)
        _, entry_id = self._historical_class(day=19, attended=True, payable_minutes=45)
        with self.site.connect() as conn:
            conn.execute(
                "UPDATE time_entries SET status='approved',payable_minutes=45,student_count=1,"
                "amount_satang=30000,pricing_type='manual_amount' WHERE id=?",
                (entry_id,),
            )
        with patch.object(self.site, "enqueue_notice", side_effect=RuntimeError("synthetic outbox failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic outbox failure"):
                self._request("admin", "POST", "/api/admin/individual-billing",
                              {"period": self.period})
        with self.site.connect() as conn:
            for table in ("invoices", "individual_monthly_invoices", "individual_billing_lines"):
                self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
        retried = self._request("admin", "POST", "/api/admin/individual-billing",
                                {"period": self.period})
        self.assertEqual(retried.status, 200, retried.body)
        self.assertEqual(len(retried.body["issued"]), 1)
        self.assertEqual(retried.body["total_satang"], 22_500)


if __name__ == "__main__":
    unittest.main()
