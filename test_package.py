"""Offline integration checks using SQLite as a Turso transport stand-in.

These checks exercise application boundaries, not the real Turso network or a
Vercel deployment. Run with `python3 -m unittest test_package.py`.
"""

from __future__ import annotations

import sqlite3
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as site
import remote
from backend import ApiError, hash_password, uid


TEST_SLIP = base64.b64encode(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)).decode("ascii")


class DeploymentPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        database = Path(self.temp.name) / "remote-stand-in.sqlite3"

        def sqlite_transport(_url, *, auth_token=None, isolation_level=None):
            return sqlite3.connect(database, isolation_level=isolation_level)

        connection_patch = patch.object(remote.turso_serverless, "connect", sqlite_transport)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)

        self.owner = remote.RemotePhysicsPlusApp(
            "turso://test.example", "synthetic-test-token", "https://school.example"
        )
        self.owner.initialize_remote()
        backend_patch = patch.object(site, "_backend", lambda: self.owner)
        backend_patch.start()
        self.addCleanup(backend_patch.stop)
        self.base_url = "https://school.example"

    def test_schema_seed_and_single_administrator(self) -> None:
        self.owner.initialize_remote()
        status = self.owner.check_remote()
        self.assertTrue(status["schema_initialized"])
        self.assertEqual(status["confirmed_default_rate_count"], 8)
        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")
        with self.assertRaises(ApiError):
            self.owner.bootstrap_admin(None, "Admin 2", "SafeTestPassword123!", "other-admin")
        self.assertEqual(self.owner.check_remote()["admin_count"], 1)

    def test_admin_reauthenticated_migration_is_idempotent(self) -> None:
        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")
        client = site.app.test_client()
        login = client.post("/api/auth/login", base_url=self.base_url,
                            headers={"Origin": self.base_url},
                            json={"login": "physicsplus", "password": "SafeTestPassword123!"})
        headers = {"Origin": self.base_url, "X-CSRF-Token": login.json["csrf_token"]}
        route = "/api/admin/maintenance/initialize"
        self.assertEqual(client.post(route, base_url=self.base_url, headers=headers,
                                     json={"current_password": "wrong"}).status_code, 401)
        self.assertEqual(client.post(route, base_url=self.base_url, headers=headers,
                                     json={"current_password": "SafeTestPassword123!"}).status_code, 200)
        teachers = [u for u in client.get("/api/admin/users", base_url=self.base_url).json["users"]
                    if u["role"] == "teacher"]
        self.assertEqual(len(teachers), 3)

    def test_private_dashboard_login_and_student_registration(self) -> None:
        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")
        client = site.app.test_client()
        self.assertEqual(client.get("/dashboard.html", base_url=self.base_url).status_code, 303)
        self.assertEqual(client.get("/private/dashboard.html", base_url=self.base_url).status_code, 404)

        bad_origin = client.post(
            "/api/auth/login",
            base_url=self.base_url,
            headers={"Origin": "https://attacker.example"},
            json={"login": "physicsplus", "password": "SafeTestPassword123!"},
        )
        self.assertEqual(bad_origin.status_code, 403)

        login = client.post(
            "/api/auth/login",
            base_url=self.base_url,
            headers={"Origin": self.base_url},
            json={"login": "physicsplus", "password": "SafeTestPassword123!"},
        )
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json["user"]["role"], "admin")
        cookie = login.headers["Set-Cookie"]
        for attribute in ("Secure", "HttpOnly", "SameSite=Lax"):
            self.assertIn(attribute, cookie)
        dashboard = client.get("/dashboard.html", base_url=self.base_url)
        self.assertEqual(dashboard.status_code, 200)
        dashboard.close()
        self.assertEqual(client.get("/api/admin/reports/students", base_url=self.base_url).status_code, 200)

        student = site.app.test_client()
        registration = student.post(
            "/api/auth/register",
            base_url=self.base_url,
            headers={"Origin": self.base_url},
            json={
                "name": "Example Student", "email": "student@example.com",
                "password": "SafeTestPassword123!", "grade": "M3", "school": "Example School",
                "guardian_name": "Example Guardian", "guardian_phone": "0812345678",
                "guardian_relationship": "mother",
            },
        )
        self.assertEqual(registration.status_code, 201)
        self.assertEqual(registration.json["user"]["role"], "student")
        self.assertEqual(student.get("/api/me", base_url=self.base_url).json["user"]["role"], "student")
        self.assertEqual(student.get("/admin", base_url=self.base_url).status_code, 403)

    def test_throttle_persists_across_app_instances(self) -> None:
        second_instance = remote.RemotePhysicsPlusApp(
            "turso://test.example", "synthetic-test-token", "https://school.example"
        )
        for _ in range(5):
            self.owner.limiter.check("login:shared@example.com", 10, 900)
        for _ in range(5):
            second_instance.limiter.check("login:shared@example.com", 10, 900)
        with self.assertRaises(ApiError) as caught:
            second_instance.limiter.check("login:shared@example.com", 10, 900)
        self.assertEqual(caught.exception.status, 429)

    def test_connection_rolls_back_and_closes(self) -> None:
        with self.assertRaises(RuntimeError):
            with self.owner.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO login_throttle(key_hash,window_started,attempts) VALUES('test',1,1)"
                )
                raise RuntimeError("trigger rollback")
        with self.owner.connect() as conn:
            self.assertIsNone(
                conn.execute("SELECT 1 FROM login_throttle WHERE key_hash='test'").fetchone()
            )

    def test_administrator_controls_student_invoice_amount(self) -> None:
        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")
        admin = site.app.test_client()
        login = admin.post("/api/auth/login", base_url=self.base_url,
                           headers={"Origin": self.base_url},
                           json={"login": "physicsplus", "password": "SafeTestPassword123!"})
        admin_headers = {"Origin": self.base_url, "X-CSRF-Token": login.json["csrf_token"]}
        teacher = admin.post("/api/admin/staff", base_url=self.base_url, headers=admin_headers,
                             json={"name": "Example Teacher", "email": "teacher@example.com", "role": "teacher"})
        self.assertEqual(teacher.status_code, 201, teacher.json)
        course = admin.post("/api/admin/courses", base_url=self.base_url, headers=admin_headers,
                            json={"subject": "physics", "level": "high",
                                  "teacher_id": teacher.json["user"]["id"], "capacity": 10,
                                  "price_satang": 450_000})
        self.assertEqual(course.status_code, 201, course.json)
        student = site.app.test_client()
        registration = student.post("/api/auth/register", base_url=self.base_url,
                                    headers={"Origin": self.base_url},
                                    json={"name": "Example Student", "email": "student@example.com",
                                          "password": "SafeTestPassword123!", "grade": "M4", "school": "Example School",
                                          "guardian_name": "Example Guardian", "guardian_phone": "0812345678",
                                          "guardian_relationship": "mother"})
        self.assertEqual(registration.status_code, 201, registration.json)
        student_headers = {"Origin": self.base_url, "X-CSRF-Token": registration.json["csrf_token"]}
        enrollment = student.post("/api/student/enrollments", base_url=self.base_url, headers=student_headers,
                                  json={"course_id": course.json["course_id"], "grade": "M4",
                                        "school": "Example School", "guardian_name": "Example Guardian",
                                        "guardian_phone": "0812345678", "guardian_relationship": "mother"})
        self.assertEqual(enrollment.status_code, 201, enrollment.json)
        self.assertNotIn("invoice", enrollment.json)
        self.assertEqual(student.get("/api/student/dashboard", base_url=self.base_url).json["invoices"], [])
        self.assertEqual(student.get("/api/admin/invoices", base_url=self.base_url).status_code, 403)
        invoice = admin.post("/api/admin/invoices", base_url=self.base_url, headers=admin_headers,
                             json={"enrollment_id": enrollment.json["enrollment_id"],
                                   "amount_satang": 175000, "due_date": "2030-01-15"})
        self.assertEqual(invoice.status_code, 201, invoice.json)
        self.assertEqual(student.get("/api/student/dashboard", base_url=self.base_url).json["invoices"][0]["amount_satang"], 175000)
        altered = student.post("/api/student/payment-claims", base_url=self.base_url, headers=student_headers,
                               json={"invoice_id": invoice.json["invoice_id"], "amount_satang": 1,
                                     "slip_mime": "image/png", "slip_base64": TEST_SLIP})
        self.assertEqual(altered.status_code, 400, altered.json)
        claim = student.post("/api/student/payment-claims", base_url=self.base_url, headers=student_headers,
                             json={"invoice_id": invoice.json["invoice_id"],
                                   "slip_mime": "image/png", "slip_base64": TEST_SLIP})
        self.assertEqual(claim.status_code, 201, claim.json)
        self.assertEqual(claim.json["amount_satang"], 175000)
        alerts_url = "/api/admin/payment-alerts"
        self.assertEqual(student.get(alerts_url, base_url=self.base_url).status_code, 403)
        self.assertEqual(site.app.test_client().get(alerts_url, base_url=self.base_url).status_code, 401)
        alerts = admin.get(alerts_url, base_url=self.base_url)
        self.assertEqual(alerts.status_code, 200, alerts.json)
        self.assertEqual(alerts.json["pending_count"], 1)
        self.assertEqual(alerts.json["pending_payment_claims"][0]["id"], claim.json["claim_id"])
        self.assertEqual(alerts.json["pending_payment_claims"][0]["student_name"], "Example Student")
        self.assertEqual(alerts.json["pending_payment_claims"][0]["amount_satang"], 175000)
        self.assertNotIn("guardian_phone", str(alerts.json))
        approved = admin.post(f"/api/admin/payment-claims/{claim.json['claim_id']}/approve",
                              base_url=self.base_url, headers=admin_headers, json={})
        self.assertEqual(approved.status_code, 200, approved.json)
        after_review = admin.get(alerts_url, base_url=self.base_url).json
        self.assertEqual(after_review, {"pending_payment_claims": [], "pending_count": 0})

    def test_payment_receipts_follow_course_purchase_and_are_private(self) -> None:
        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")
        admin = site.app.test_client()
        login = admin.post("/api/auth/login", base_url=self.base_url,
                           headers={"Origin": self.base_url},
                           json={"login": "physicsplus", "password": "SafeTestPassword123!"})
        admin_headers = {"Origin": self.base_url, "X-CSRF-Token": login.json["csrf_token"]}
        course = next(item for item in admin.get("/api/courses", base_url=self.base_url).json["courses"]
                      if item["subject"] == "physics")
        student = site.app.test_client()
        registration = student.post("/api/auth/register", base_url=self.base_url,
                                    headers={"Origin": self.base_url},
                                    json={"name": "Test Student", "email": "receipt-student@example.com",
                                          "password": "SafeTestPassword123!", "grade": "M4", "school": "Test School",
                                          "guardian_name": "Test Guardian", "guardian_phone": "0812345678",
                                          "guardian_relationship": "mother"})
        self.assertEqual(registration.status_code, 201, registration.json)
        student_headers = {"Origin": self.base_url, "X-CSRF-Token": registration.json["csrf_token"]}
        enrollment = student.post("/api/student/enrollments", base_url=self.base_url,
                                  headers=student_headers,
                                  json={"course_id": course["id"], "grade": "M4", "school": "Test School",
                                        "guardian_name": "Test Guardian", "guardian_phone": "0812345678",
                                        "guardian_relationship": "mother"})
        self.assertEqual(enrollment.status_code, 201, enrollment.json)
        invoice = admin.post("/api/admin/invoices", base_url=self.base_url, headers=admin_headers,
                             json={"enrollment_id": enrollment.json["enrollment_id"],
                                   "amount_satang": 450_000, "due_date": "2030-01-15"})
        self.assertEqual(invoice.status_code, 201, invoice.json)
        invoice_id = invoice.json["invoice_id"]
        first = admin.post("/api/admin/payments", base_url=self.base_url, headers=admin_headers,
                           json={"invoice_id": invoice_id, "amount_satang": 150_000,
                                 "method": "bank_transfer", "reference": "RECEIPT-REF-1"})
        self.assertEqual(first.status_code, 201, first.json)
        first_receipt_id = first.json["receipt_id"]
        self.assertEqual(admin.get("/api/admin/invoices", base_url=self.base_url).json["receipts"][0]["id"], first_receipt_id)
        dashboard = student.get("/api/student/dashboard", base_url=self.base_url).json
        self.assertEqual(len(dashboard["receipts"]), 1)
        self.assertEqual(dashboard["receipts"][0]["course_title"], course["title"])
        self.assertEqual(dashboard["receipts"][0]["course_code"], course["course_code"])
        self.assertEqual(dashboard["receipts"][0]["enrollment_id"], enrollment.json["enrollment_id"])
        self.assertEqual(dashboard["receipts"][0]["amount_satang"], 150_000)
        document = student.get(f"/api/receipts/{first_receipt_id}.pdf", base_url=self.base_url)
        self.assertEqual(document.status_code, 200, document.json if document.is_json else "")
        self.assertTrue(document.data.startswith(b"%PDF-"))
        self.assertIn("application/pdf", document.content_type)
        self.assertIn(first.json["receipt_number"], document.headers["Content-Disposition"])
        self.assertIn("no-store", document.headers["Cache-Control"])
        claim = student.post("/api/student/payment-claims", base_url=self.base_url,
                             headers=student_headers,
                             json={"invoice_id": invoice_id,
                                   "slip_mime": "image/png", "slip_base64": TEST_SLIP})
        self.assertEqual(claim.status_code, 201, claim.json)
        self.assertEqual(claim.json["amount_satang"], 300_000)
        self.assertEqual(len(student.get("/api/student/dashboard", base_url=self.base_url).json["receipts"]), 1)
        approval = admin.post(f"/api/admin/payment-claims/{claim.json['claim_id']}/approve",
                              base_url=self.base_url, headers=admin_headers, json={})
        self.assertEqual(approval.status_code, 200, approval.json)
        receipts = student.get("/api/student/dashboard", base_url=self.base_url).json["receipts"]
        self.assertEqual(len(receipts), 2)
        self.assertEqual(sum(item["amount_satang"] for item in receipts), 450_000)
        self.assertEqual({item["course_code"] for item in receipts}, {course["course_code"]})
        self.assertEqual(admin.post(f"/api/admin/payment-claims/{claim.json['claim_id']}/approve",
                                    base_url=self.base_url, headers=admin_headers, json={}).status_code, 404)
        self.owner.initialize_remote()
        self.assertEqual(len(student.get("/api/student/dashboard", base_url=self.base_url).json["receipts"]), 2)

        other = site.app.test_client()
        self.assertEqual(other.post("/api/auth/register", base_url=self.base_url,
                                    headers={"Origin": self.base_url},
                                    json={"name": "Other Student", "email": "other-student@example.com",
                                          "password": "SafeTestPassword123!", "grade": "M4", "school": "Test School",
                                          "guardian_name": "Test Guardian", "guardian_phone": "0812345678",
                                          "guardian_relationship": "mother"}).status_code, 201)
        self.assertEqual(other.get(f"/api/receipts/{first_receipt_id}", base_url=self.base_url).status_code, 404)
        self.assertEqual(other.get(f"/api/receipts/{first_receipt_id}.pdf", base_url=self.base_url).status_code, 404)
        teacher_id = uid()
        with self.owner.connect() as conn:
            conn.execute("INSERT INTO users(id,email,name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                         (teacher_id, "receipt-teacher@example.com", "Test Teacher",
                          hash_password("SafeTestPassword123!"), "teacher", 1))
        teacher = site.app.test_client()
        self.assertEqual(teacher.post("/api/auth/login", base_url=self.base_url,
                                      headers={"Origin": self.base_url},
                                      json={"login": "receipt-teacher@example.com",
                                            "password": "SafeTestPassword123!"}).status_code, 200)
        self.assertEqual(teacher.get(f"/api/receipts/{first_receipt_id}.pdf", base_url=self.base_url).status_code, 403)
        self.assertEqual(site.app.test_client().get(f"/api/receipts/{first_receipt_id}.pdf",
                                                     base_url=self.base_url).status_code, 401)

    def test_receipt_migration_backfills_existing_confirmed_payment_once(self) -> None:
        self.owner.bootstrap_admin(None, "Admin", "SafeTestPassword123!", "physicsplus")
        with self.owner.connect() as conn:
            admin_id = conn.execute("SELECT id FROM users WHERE role='admin'").fetchone()[0]
            course = conn.execute(
                "SELECT c.id,c.course_code,c.title FROM courses c "
                "LEFT JOIN course_formats f ON f.course_id=c.id "
                "WHERE c.subject='physics' AND f.course_id IS NULL LIMIT 1"
            ).fetchone()
            student_id, enrollment_id, invoice_id, payment_id = (uid() for _ in range(4))
            conn.execute("INSERT INTO users(id,email,name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                         (student_id, "old-student@example.com", "Old Student",
                          hash_password("SafeTestPassword123!"), "student", 1))
            conn.execute("INSERT INTO enrollments(id,course_id,student_id,created_at) VALUES(?,?,?,?)",
                         (enrollment_id, course["id"], student_id, 1))
            conn.execute("INSERT INTO invoices(id,invoice_number,enrollment_id,student_id,description,amount_satang,status,due_date,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (invoice_id, "PP-OLD-001", enrollment_id, student_id,
                          "ค่าเรียนเดิม", 450_000, "partial", "2030-01-15", 1))
            conn.execute("INSERT INTO payments(id,invoice_id,amount_satang,method,reference,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?)",
                         (payment_id, invoice_id, 100_000, "cash", "LEGACY-REF", admin_id, 2))
        self.owner.initialize_remote()
        self.owner.initialize_remote()
        with self.owner.connect() as conn:
            rows = conn.execute("SELECT payment_id,course_code,course_title,amount_satang FROM receipts WHERE invoice_id=?",
                                (invoice_id,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0]), (payment_id, course["course_code"], course["title"], 100_000))


if __name__ == "__main__":
    unittest.main()
