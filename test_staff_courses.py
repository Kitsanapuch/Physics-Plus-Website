"""Business and security checks for editable courses and teacher roster accounts."""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend import (PhysicsPlusApp, BRAND_CODES_MIGRATION, COURSE_CATALOG, SCHEMA,
                     migrate_academic_schema, migrate_brand_codes,
                     migrate_nullable_course_teachers, seed_initial_courses,
                     seed_individual_courses)


class StaffCourseTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.owner = PhysicsPlusApp(Path(directory.name) / "school.sqlite3", "https://school.example")
        self.owner.bootstrap_admin(None, "Administrator", "PreviousPassword123!", "oldadmin")
        self.base = {"host": "school.example", "origin": "https://school.example"}
        login = self.owner.handle("POST", "/api/auth/login",
                                  {"login": "oldadmin", "password": "PreviousPassword123!"},
                                  self.base, "127.0.0.1")
        self.assertEqual(login.status, 200)
        self.auth = {**self.base, "cookie": login.headers["Set-Cookie"].split(";", 1)[0],
                     "x-csrf-token": login.body["csrf_token"]}

    def request(self, method, path, data=None):
        return self.owner.handle(method, path, data, self.auth, "127.0.0.1")

    def test_initial_catalog_is_live_unassigned_and_admin_prices_survive_reinitialization(self):
        listed = self.request("GET", "/api/courses").body["courses"]
        self.assertEqual(len(listed), 16)
        self.assertEqual({(row["level"], row["subject"]) for row in listed}, set(COURSE_CATALOG))
        self.assertTrue(all(row["teacher_name"] is None for row in listed))
        groups = [row for row in listed if row["course_format"] == "group"]
        individuals = [row for row in listed if row["course_format"] == "individual"]
        self.assertEqual(len(groups), len(COURSE_CATALOG))
        self.assertEqual(len(individuals), len(COURSE_CATALOG))
        self.assertEqual({(row["level"], row["subject"]) for row in individuals}, set(COURSE_CATALOG))
        self.assertTrue(all(row["price_satang"] == (350_000 if row["level"] == "middle" else 450_000)
                            for row in groups))
        self.assertTrue(all(row["price_satang"] == 30_000 for row in individuals))
        self.assertTrue(all("-ONE-" in row["course_code"] for row in individuals))
        self.assertEqual(len({row["course_code"] for row in listed}), len(listed))
        target = next(row for row in groups if row["subject"] == "physics")
        updated = self.request("PUT", f"/api/admin/courses/{target['id']}",
                               {"price_satang": 475_000})
        self.assertEqual(updated.status, 200, updated.body)
        with self.owner.connect() as conn:
            seed_initial_courses(conn)
            seed_individual_courses(conn)
        self.owner._init_db()
        updated_list = self.request("GET", "/api/courses").body["courses"]
        self.assertEqual(len(updated_list), 16)
        self.assertEqual(next(row for row in updated_list if row["id"] == target["id"])["price_satang"], 475_000)
        admin_data = self.request("GET", "/api/admin/academic").body
        academic = admin_data["courses"]
        self.assertEqual(len(academic), 16)
        self.assertEqual(admin_data["rooms"], ["ห้องใหญ่", "ห้องเล็กด้านหน้า", "ห้องเล็กด้านหลัง", "ห้องเล็กหลังห้องใหญ่"])
        self.assertTrue(all(row["teacher_id"] is None for row in academic))
        report = self.request("GET", "/api/admin/reports/courses").body["rows"]
        self.assertEqual(len(report), 16)
        self.assertTrue(all(row["teacher_name"] is None for row in report))

    def test_individual_seed_preserves_existing_owner_course_and_marks_sheet_dirty(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA foreign_keys=ON")
        seed_initial_courses(conn)
        conn.execute(
            "INSERT INTO courses(id,course_code,subject_code,title,subject,level,price_satang,"
            "teacher_id,capacity,created_at) VALUES(?,?,?,?,?,?,?,NULL,?,?)",
            ("custom-individual", "PP-H-PHY-ONE-000042", "H-PHY", "ฟิสิกส์ ม.ปลาย · ตัวต่อตัว",
             "physics", "high", 30_000, 7, 1),
        )
        conn.execute("INSERT INTO course_formats(course_id,format) VALUES(?,'individual')",
                     ("custom-individual",))
        conn.execute(
            "INSERT INTO courses(id,course_code,subject_code,title,subject,level,price_satang,"
            "teacher_id,capacity,active,created_at) VALUES(?,?,?,?,?,?,?,NULL,?,0,?)",
            ("archived-individual", "PP-H-CHE-ONE-000042", "H-CHE", "เคมี ม.ปลาย · ตัวต่อตัว",
             "chemistry", "high", 30_000, 5, 1),
        )
        conn.execute("INSERT INTO course_formats(course_id,format) VALUES(?,'individual')",
                     ("archived-individual",))
        conn.execute(
            "INSERT INTO sheet_sync_state(dataset,revision,synced_revision,updated_at) "
            "VALUES('courses',4,4,1)",
        )
        seed_individual_courses(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM course_formats").fetchone()[0], 9)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM courses c JOIN course_formats f ON f.course_id=c.id "
            "WHERE c.active=1"
        ).fetchone()[0], 8)
        self.assertEqual(conn.execute("SELECT capacity FROM courses WHERE id='custom-individual'").fetchone()[0], 7)
        self.assertEqual(conn.execute("SELECT active FROM courses WHERE id='archived-individual'").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT revision,synced_revision FROM sheet_sync_state WHERE dataset='courses'").fetchone()[:], (5, 4))
        seed_individual_courses(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM course_formats").fetchone()[0], 9)
        self.assertEqual(conn.execute("SELECT revision FROM sheet_sync_state WHERE dataset='courses'").fetchone()[0], 5)

    def test_existing_course_rebuild_keeps_child_records_and_foreign_keys(self):
        legacy_schema = SCHEMA.replace("teacher_id TEXT REFERENCES users(id),\n  capacity",
                                       "teacher_id TEXT NOT NULL REFERENCES users(id),\n  capacity")
        self.assertNotEqual(legacy_schema, SCHEMA)
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        conn.executescript(legacy_schema)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("INSERT INTO users(id,email,name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                     ("teacher", "teacher@example.com", "Teacher", "unused", "teacher", 1))
        conn.execute("INSERT INTO users(id,email,name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                     ("student", "student@example.com", "Student", "unused", "student", 1))
        conn.execute(
            "INSERT INTO courses(id,course_code,subject_code,title,subject,level,price_satang,teacher_id,capacity,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("old-course", "PP-H-PHY-000001", "H-PHY", "ฟิสิกส์ ม.ปลาย", "physics", "high",
             525_000, "teacher", 13, 1),
        )
        conn.execute("INSERT INTO enrollments(id,course_id,student_id,created_at) VALUES(?,?,?,?)",
                     ("enrollment", "old-course", "student", 1))
        conn.execute(
            "INSERT INTO class_sessions(id,course_id,room_name,teacher_id,starts_at,ends_at,created_at) "
            "VALUES(?,?,?,?,?,?,?)", ("class", "old-course", "A", "teacher", 100, 200, 1),
        )
        conn.execute(
            "INSERT INTO invoices(id,invoice_number,enrollment_id,student_id,description,amount_satang,due_date,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("invoice", "PP-20261021-ABC", "enrollment", "student", "Tuition", 525_000,
             "2026-10-31", 1),
        )
        migrate_nullable_course_teachers(conn)
        migrate_nullable_course_teachers(conn)
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(next(row[3] for row in conn.execute("PRAGMA table_info(courses)")
                              if row[1] == "teacher_id"), 0)
        self.assertEqual(tuple(conn.execute("SELECT teacher_id,price_satang FROM courses WHERE id='old-course'").fetchone()),
                         ("teacher", 525_000))
        self.assertEqual(conn.execute("SELECT course_id FROM enrollments WHERE id='enrollment'").fetchone()[0], "old-course")
        self.assertEqual(conn.execute("SELECT course_id FROM class_sessions WHERE id='class'").fetchone()[0], "old-course")
        self.assertEqual(conn.execute("SELECT amount_satang FROM invoices WHERE id='invoice'").fetchone()[0], 525_000)
        conn.execute(
            "INSERT INTO courses(id,course_code,subject_code,title,subject,level,price_satang,teacher_id,capacity,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("new-course", "PP-M-MAT-000001", "M-MAT", "คณิตศาสตร์ ม.ต้น", "math", "middle",
             350_000, None, 13, 2),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO enrollments(id,course_id,student_id,created_at) VALUES(?,?,?,?)",
                         ("orphan", "missing", "student", 2))

    def test_pending_roster_is_assignable_but_cannot_log_in_until_activation(self):
        users = self.request("GET", "/api/admin/users").body["users"]
        seeded = [row for row in users if row["role"] == "teacher"]
        self.assertEqual({row["name"] for row in seeded}, {"ครูตุ้ม", "ครูโรจน์", "ครูโจ้"})
        self.assertTrue(all(row["email"] is None and row["roster_pending"] for row in seeded))
        self.owner._init_db()
        self.assertEqual(len([row for row in self.request("GET", "/api/admin/users").body["users"] if row["role"] == "teacher"]), 3)

        teacher = seeded[0]
        create = self.request("POST", "/api/admin/courses", {"level": "high", "subject": "physics",
                             "price_satang": 525_000, "teacher_id": teacher["id"], "capacity": 13})
        self.assertEqual(create.status, 201, create.body)
        self.assertEqual(create.body["price_satang"], 525_000)
        course = self.request("PUT", f"/api/admin/courses/{create.body['course_id']}", {"price_satang": 575_000})
        self.assertEqual(course.status, 200, course.body)
        self.assertEqual(course.body["price_satang"], 575_000)
        with self.owner.connect() as conn:
            migrate_academic_schema(conn)
            self.assertEqual(conn.execute("SELECT active,price_satang FROM courses WHERE id=?", (create.body["course_id"],)).fetchone()[:], (1, 575_000))

        for action in ("enable", "reset-password"):
            result = self.request("POST", f"/api/admin/users/{teacher['id']}/{action}", {})
            self.assertEqual(result.status, 409)
        invalid = self.request("POST", f"/api/admin/staff/{teacher['id']}/activate",
                               {"email": "staff@physics-plus.invalid"})
        self.assertEqual(invalid.status, 400)
        active = self.request("POST", f"/api/admin/staff/{teacher['id']}/activate",
                              {"email": "teacher@example.com"})
        self.assertEqual(active.status, 200, active.body)
        self.assertTrue(active.body["temporary_password"])
        self.assertTrue(active.body["user"]["login_enabled"])
        again = self.request("POST", f"/api/admin/staff/{teacher['id']}/activate",
                             {"email": "another@example.com"})
        self.assertEqual(again.status, 409)

    def test_admin_controls_staff_and_subject_catalog(self):
        new = self.request("POST", "/api/admin/staff", {"name": "ครูใหม่"})
        self.assertEqual(new.status, 201, new.body)
        self.assertNotIn("temporary_password", new.body)
        self.assertEqual(new.body["user"]["email"], None)
        duplicate = self.request("POST", "/api/admin/staff", {"name": "ครูใหม่"})
        self.assertEqual(duplicate.status, 409)
        teacher_id = new.body["user"]["id"]
        edited = self.request("PUT", f"/api/admin/users/{teacher_id}", {"name": "ครูใหม่ A"})
        self.assertEqual(edited.status, 200)
        self.assertEqual(edited.body["email"], None)
        self.assertEqual(self.request("PUT", f"/api/admin/users/{teacher_id}",
                                      {"email": "teacher@example.com"}).status, 409)
        self.assertEqual(self.request("POST", "/api/admin/courses", {"level": "middle", "subject": "physics",
                         "price_satang": 350_000, "teacher_id": teacher_id, "capacity": 10}).status, 400)
        report = self.request("GET", "/api/admin/reports/teachers").body["rows"]
        self.assertTrue(all(not str(row["email"] or "").endswith(".invalid") for row in report))

    def test_admin_username_password_rotation_revokes_old_login(self):
        changed = self.request("POST", "/api/admin/account", {
            "current_password": "PreviousPassword123!", "new_username": "physicsplus",
            "new_password": "NewSyntheticPassword456!",
        })
        self.assertEqual(changed.status, 200, changed.body)
        self.assertTrue(changed.body["sessions_revoked"])
        old = self.owner.handle("POST", "/api/auth/login",
                                {"login": "oldadmin", "password": "PreviousPassword123!"},
                                self.base, "127.0.0.1")
        self.assertEqual(old.status, 401)
        new = self.owner.handle("POST", "/api/auth/login",
                                {"login": "physicsplus", "password": "NewSyntheticPassword456!"},
                                self.base, "127.0.0.1")
        self.assertEqual(new.status, 200)

    def test_brand_migration_changes_display_codes_without_changing_record_ids(self):
        teacher = next(row for row in self.request("GET", "/api/admin/users").body["users"]
                       if row["role"] == "teacher")
        created = self.request("POST", "/api/admin/courses", {"level": "middle", "subject": "math",
                               "price_satang": 378_000, "teacher_id": teacher["id"], "capacity": 10})
        self.assertEqual(created.status, 201)
        with self.owner.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM app_migrations WHERE key=?", (BRAND_CODES_MIGRATION,))
            conn.execute("UPDATE users SET public_code='PH-'||substr(public_code,4) WHERE id=?", (teacher["id"],))
            conn.execute("UPDATE courses SET course_code='PH-'||substr(course_code,4) WHERE id=?", (created.body["course_id"],))
            conn.execute(
                "INSERT INTO invoices(id,invoice_number,student_id,description,amount_satang,due_date,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                ("legacy-invoice", "PH-20261001-ABC123", teacher["id"], "Earlier invoice", 350_000,
                 "2026-10-31", 1),
            )
            conn.commit()
            migrate_brand_codes(conn)
            row = conn.execute("SELECT id,teacher_id,course_code,price_satang FROM courses WHERE id=?",
                               (created.body["course_id"],)).fetchone()
            self.assertEqual(row["id"], created.body["course_id"])
            self.assertEqual(row["teacher_id"], teacher["id"])
            self.assertEqual(row["price_satang"], 378_000)
            self.assertTrue(row["course_code"].startswith("PP-"))
            self.assertTrue(conn.execute("SELECT public_code FROM users WHERE id=?", (teacher["id"],)).fetchone()[0].startswith("PP-"))
            self.assertEqual(conn.execute("SELECT invoice_number FROM invoices WHERE id='legacy-invoice'").fetchone()[0],
                             "PH-20261001-ABC123")


if __name__ == "__main__":
    unittest.main()
