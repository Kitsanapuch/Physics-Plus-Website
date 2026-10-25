#!/usr/bin/env python3
"""Physics Plus domain API and SQLite-compatible query layer.

The Vercel package subclasses PhysicsPlusApp with a remote Turso connection.
This module contains no HTTP server entrypoint and stores no credentials.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo


BANGKOK = ZoneInfo("Asia/Bangkok")
SESSION_SECONDS = 7 * 24 * 3600
MAX_BODY = 64 * 1024
EMAIL_LOCAL_RE = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+\Z", re.ASCII)
EMAIL_DOMAIN_LABEL_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z", re.ASCII)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
GRADES = {f"M{grade}" for grade in range(1, 7)}
GUARDIAN_RELATIONSHIPS = {"father", "mother", "guardian", "other"}
COURSE_CATALOG = {
    ("middle", "science"): ("M-SCI", "วิทยาศาสตร์"),
    ("middle", "math"): ("M-MAT", "คณิตศาสตร์"),
    ("middle", "english"): ("M-ENG", "ภาษาอังกฤษ"),
    ("high", "physics"): ("H-PHY", "ฟิสิกส์"),
    ("high", "chemistry"): ("H-CHE", "เคมี"),
    ("high", "biology"): ("H-BIO", "ชีววิทยา"),
    ("high", "math"): ("H-MAT", "คณิตศาสตร์"),
    ("high", "english"): ("H-ENG", "ภาษาอังกฤษ"),
}
INTERNAL_STAFF_DOMAIN = "physics-plus.invalid"
TEACHER_ROSTER_MIGRATION = "teacher_roster_2026_10_21"
BRAND_CODES_MIGRATION = "physics_plus_codes_2026_10_21"
STUDENT_CODE_MIGRATION = "student_codes_yy_nnn_2026_10_24"
COURSE_SEED_MIGRATION = "physics_plus_initial_courses_2026_10_21"
INITIAL_TEACHERS = ("ครูตุ้ม", "ครูโรจน์", "ครูโจ้")
INITIAL_COURSE_CAPACITY = 13
DEFAULT_PAYMENT_ACCOUNT = ("ttb", "ทีเอ็มบีธนชาต", "ชดาภัษท สุดศิริ", "585-2-22394-9")
SHEET_DATASETS = (
    "students", "teachers", "courses", "classes", "class_rosters",
    "enrollments", "invoices", "payments", "receipts", "claims",
    "teaching", "payroll", "rates", "payroll_monthly", "payroll_summary", "payment_account",
)
SHEET_EVENT_DATASETS = {
    "user": ("students", "teachers", "courses", "enrollments", "invoices", "receipts", "claims", "classes", "class_rosters", "teaching", "payroll", "payroll_monthly", "payroll_summary"),
    "student_profile": ("students",),
    "course": ("courses", "classes", "class_rosters", "enrollments", "teaching"),
    "enrollment": ("students", "enrollments", "class_rosters"),
    "class_session": ("classes", "class_rosters", "teaching", "payroll_monthly", "payroll_summary"),
    "attendance": ("class_rosters",),
    "rate_rule": ("rates", "payroll_monthly", "payroll_summary"),
    "rate_schedule": ("rates", "payroll_monthly", "payroll_summary"),
    "time_entry": ("teaching", "payroll_monthly", "payroll_summary"),
    "invoice": ("invoices",),
    "payment_claim": ("claims",),
    "payment": ("payments", "receipts", "invoices"),
    "payroll_run": ("payroll", "teaching", "payroll_monthly", "payroll_summary"),
    "payroll_adjustment": ("payroll", "payroll_monthly", "payroll_summary"),
    "payment_account": ("payment_account",),
}


def pending_teacher_email(user_id: str) -> str:
    """Internal, undeliverable key for a roster entry without login credentials."""
    return f"staff-{user_id}@{INTERNAL_STAFF_DOMAIN}"


def is_pending_teacher(row: sqlite3.Row | dict) -> bool:
    email = row["email"]
    return bool(row["role"] == "teacher" and isinstance(email, str)
                and email == pending_teacher_email(row["id"]))


def public_email(row: sqlite3.Row | dict) -> str | None:
    return None if is_pending_teacher(row) else row["email"]


def eligible_teacher(conn: sqlite3.Connection, teacher_id: str) -> bool:
    """Only active teachers and intentionally pending roster records can be assigned."""
    teacher = conn.execute("SELECT id,email,role,status FROM users WHERE id=?", (teacher_id,)).fetchone()
    return bool(teacher and teacher["role"] == "teacher" and
                (teacher["status"] == "active" or is_pending_teacher(teacher)))


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


@dataclass
class Response:
    status: int
    body: dict
    headers: dict[str, str] = field(default_factory=dict)


def fail(status: int, code: str, message: str) -> None:
    raise ApiError(status, code, message)


def now() -> int:
    return int(time.time())


def uid() -> str:
    return uuid.uuid4().hex


def required_str(data: dict, key: str, max_length: int = 200) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        fail(400, "invalid_input", f"{key} is required")
    value = value.strip()
    if not value or len(value) > max_length:
        fail(400, "invalid_input", f"{key} must be 1–{max_length} characters")
    return value


def optional_str(data: dict, key: str, max_length: int = 200) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        return None
    return required_str(data, key, max_length)


def required_int(data: dict, key: str, low: int, high: int) -> int:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < low or value > high:
        fail(400, "invalid_input", f"{key} must be an integer from {low} to {high}")
    return value


def normalize_email(value: str) -> str:
    value = value.strip()
    if len(value) > 254 or not value.isascii() or value.count("@") != 1:
        fail(400, "invalid_email", "Enter a valid email address")
    local, domain = value.split("@")
    labels = domain.split(".")
    if not (1 <= len(local) <= 64) or not EMAIL_LOCAL_RE.fullmatch(local):
        fail(400, "invalid_email", "Enter a valid email address")
    if local.startswith(".") or local.endswith(".") or ".." in local:
        fail(400, "invalid_email", "Enter a valid email address")
    if len(labels) < 2 or any(not EMAIL_DOMAIN_LABEL_RE.fullmatch(label) for label in labels) or len(labels[-1]) < 2 or labels[-1].casefold() == "invalid":
        fail(400, "invalid_email", "Enter a valid email address")
    return value.casefold()


def normalize_username(value: str) -> str:
    value = value.strip().casefold()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{2,39}", value):
        fail(400, "invalid_username", "Username must be 3–40 lowercase letters, digits, _ or -")
    return value


def student_profile_input(data: dict) -> dict[str, str]:
    grade = required_str(data, "grade", 2).upper()
    if grade not in GRADES:
        fail(400, "invalid_grade", "Choose a grade from M1 to M6")
    school = required_str(data, "school", 150)
    guardian_name = required_str(data, "guardian_name", 120)
    phone = re.sub(r"[\s-]", "", required_str(data, "guardian_phone", 24))
    if not re.fullmatch(r"(?:0\d{8,9}|\+66\d{8,9})", phone):
        fail(400, "invalid_guardian_phone", "Enter a valid Thai contact number")
    relationship = required_str(data, "guardian_relationship", 20)
    if relationship not in GUARDIAN_RELATIONSHIPS:
        fail(400, "invalid_guardian_relationship", "Choose a guardian relationship")
    for value in (school, guardian_name):
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            fail(400, "invalid_input", "Control characters are not allowed")
    return {"grade": grade, "school": school, "guardian_name": guardian_name,
            "guardian_phone": phone, "guardian_relationship": relationship}


def validate_password(password: object) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        fail(400, "weak_password", "Password must be 12–128 characters")
    if len(password.encode("utf-8")) > 512:
        fail(400, "weak_password", "Password is too long")
    return password


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1, maxmem=64 * 1024 * 1024)
    return "scrypt$32768$8$1$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        kind, n, r, p, salt, expected = encoded.split("$")
        if kind != "scrypt" or (int(n), int(r), int(p)) != (32768, 8, 1):
            return False
        digest = hashlib.scrypt(password.encode(), salt=base64.urlsafe_b64decode(salt), n=int(n), r=int(r), p=int(p), maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(digest, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(24))


def parse_datetime(value: str) -> int:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        fail(400, "invalid_datetime", "Use ISO 8601 date/time with timezone")
    if dt.tzinfo is None:
        fail(400, "invalid_datetime", "Date/time needs a timezone offset")
    return int(dt.timestamp())


def iso(epoch: int | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")


def bangkok_month(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, BANGKOK).strftime("%Y-%m")


def date_in_bangkok(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, BANGKOK).strftime("%Y-%m-%d")


def user_json(row: sqlite3.Row) -> dict:
    code = row["public_code"]
    return {"id": row["id"], "email": public_email(row), "username": row["username"], "name": row["name"], "role": row["role"], "status": row["status"], "must_change_password": bool(row["must_change_password"]), "student_code": code if row["role"] == "student" else None, "teacher_code": code if row["role"] == "teacher" else None}


SHEET_SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS sheet_sync_state (
  dataset TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0,
  synced_revision INTEGER NOT NULL DEFAULT 0,
  updated_at INTEGER NOT NULL, synced_at INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL DEFAULT 0,
  lease_token TEXT, lease_until INTEGER, last_error_code TEXT,
  CHECK(revision >= synced_revision AND synced_revision >= 0)
);
CREATE TABLE IF NOT EXISTS payment_account (
  id INTEGER PRIMARY KEY CHECK(id=1),
  bank_code TEXT NOT NULL, bank_name TEXT NOT NULL,
  account_name TEXT NOT NULL, account_number TEXT NOT NULL,
  updated_by TEXT REFERENCES users(id), updated_at INTEGER NOT NULL
);
"""

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE, username TEXT UNIQUE, public_code TEXT UNIQUE, name TEXT NOT NULL,
  password_hash TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('student','teacher','admin')),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','disabled')),
  must_change_password INTEGER NOT NULL DEFAULT 0 CHECK(must_change_password IN (0,1)),
  created_at INTEGER NOT NULL,
  CHECK(email IS NOT NULL OR username IS NOT NULL),
  CHECK(username IS NULL OR role='admin')
);
CREATE UNIQUE INDEX IF NOT EXISTS users_one_admin ON users(role) WHERE role='admin';
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  csrf_token TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS courses (
  id TEXT PRIMARY KEY, course_code TEXT UNIQUE, subject_code TEXT, title TEXT NOT NULL, subject TEXT NOT NULL,
  level TEXT NOT NULL CHECK(level IN ('middle','high')),
  price_satang INTEGER NOT NULL CHECK(price_satang > 0),
  teacher_id TEXT REFERENCES users(id),
  capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 100),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS enrollments (
  id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id),
  student_id TEXT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','cancelled')),
  created_at INTEGER NOT NULL, UNIQUE(course_id,student_id)
);
CREATE TABLE IF NOT EXISTS student_profiles (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  grade TEXT NOT NULL CHECK(grade IN ('M1','M2','M3','M4','M5','M6')),
  school TEXT NOT NULL,
  guardian_name TEXT NOT NULL,
  guardian_phone TEXT NOT NULL,
  guardian_relationship TEXT NOT NULL CHECK(guardian_relationship IN ('father','mother','guardian','other')),
  updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS class_sessions (
  id TEXT PRIMARY KEY, course_id TEXT NOT NULL REFERENCES courses(id),
  room_name TEXT NOT NULL, teacher_id TEXT NOT NULL REFERENCES users(id),
  starts_at INTEGER NOT NULL, ends_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','cancelled')),
  created_at INTEGER NOT NULL, CHECK(ends_at > starts_at)
);
CREATE TABLE IF NOT EXISTS session_students (
  session_id TEXT NOT NULL REFERENCES class_sessions(id),
  student_id TEXT NOT NULL REFERENCES users(id),
  PRIMARY KEY(session_id,student_id)
);
CREATE TABLE IF NOT EXISTS attendance (
  session_id TEXT NOT NULL REFERENCES class_sessions(id),
  student_id TEXT NOT NULL REFERENCES users(id),
  status TEXT NOT NULL CHECK(status IN ('present','absent')),
  recorded_by TEXT NOT NULL REFERENCES users(id), recorded_at INTEGER NOT NULL,
  PRIMARY KEY(session_id,student_id)
);
CREATE TABLE IF NOT EXISTS rate_rules (
  id TEXT PRIMARY KEY, teacher_label TEXT NOT NULL,
  teacher_id TEXT REFERENCES users(id), subject TEXT NOT NULL,
  level TEXT NOT NULL CHECK(level IN ('middle','high')),
  min_students INTEGER NOT NULL, max_students INTEGER NOT NULL,
  rate_satang_per_hour INTEGER NOT NULL CHECK(rate_satang_per_hour > 0),
  effective_from TEXT, effective_to TEXT,
  state TEXT NOT NULL DEFAULT 'pending_confirmation'
    CHECK(state IN ('pending_confirmation','confirmed','rejected')),
  source_note TEXT NOT NULL, confirmed_by TEXT REFERENCES users(id), confirmed_at INTEGER,
  CHECK(min_students > 0 AND max_students >= min_students)
);
CREATE TABLE IF NOT EXISTS time_entries (
  id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE REFERENCES class_sessions(id),
  teacher_id TEXT NOT NULL REFERENCES users(id), actual_start INTEGER NOT NULL,
  actual_end INTEGER NOT NULL, break_minutes INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'submitted'
    CHECK(status IN ('submitted','approved','rejected','payroll_locked','paid')),
  payable_minutes INTEGER, student_count INTEGER, rate_rule_id TEXT REFERENCES rate_rules(id),
  rate_satang_per_hour INTEGER, amount_satang INTEGER,
  pricing_type TEXT CHECK(pricing_type IN ('hourly_rule','manual_amount')),
  override_reason TEXT, override_reference TEXT,
  submitted_at INTEGER NOT NULL, reviewed_by TEXT REFERENCES users(id), reviewed_at INTEGER,
  rejection_reason TEXT,
  CHECK(actual_end > actual_start), CHECK(break_minutes >= 0)
);
CREATE TABLE IF NOT EXISTS invoices (
  id TEXT PRIMARY KEY, invoice_number TEXT NOT NULL UNIQUE,
  enrollment_id TEXT UNIQUE REFERENCES enrollments(id),
  student_id TEXT NOT NULL REFERENCES users(id), description TEXT NOT NULL,
  amount_satang INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'unpaid' CHECK(status IN ('unpaid','partial','paid','void')),
  due_date TEXT NOT NULL, created_at INTEGER NOT NULL, CHECK(amount_satang > 0)
);
CREATE TABLE IF NOT EXISTS payment_claims (
  id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id),
  student_id TEXT NOT NULL REFERENCES users(id), amount_satang INTEGER NOT NULL,
  reference TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','approved','rejected')),
  created_at INTEGER NOT NULL, reviewed_by TEXT REFERENCES users(id), reviewed_at INTEGER,
  CHECK(amount_satang > 0)
);
CREATE TABLE IF NOT EXISTS payments (
  id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL REFERENCES invoices(id),
  amount_satang INTEGER NOT NULL, method TEXT NOT NULL, reference TEXT NOT NULL,
  claim_id TEXT UNIQUE REFERENCES payment_claims(id),
  recorded_by TEXT NOT NULL REFERENCES users(id), recorded_at INTEGER NOT NULL,
  CHECK(amount_satang > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS payment_reference_once ON payments(invoice_id,method,reference);
CREATE TABLE IF NOT EXISTS receipts (
  id TEXT PRIMARY KEY, payment_id TEXT NOT NULL UNIQUE REFERENCES payments(id),
  receipt_number TEXT NOT NULL UNIQUE, invoice_id TEXT NOT NULL REFERENCES invoices(id),
  invoice_number TEXT NOT NULL, student_id TEXT NOT NULL REFERENCES users(id),
  student_name TEXT NOT NULL, description TEXT NOT NULL,
  enrollment_id TEXT, course_code TEXT, course_title TEXT,
  amount_satang INTEGER NOT NULL CHECK(amount_satang > 0),
  method TEXT NOT NULL, reference TEXT NOT NULL,
  paid_at INTEGER NOT NULL, issued_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS receipts_student_paid ON receipts(student_id,paid_at DESC);
CREATE TABLE IF NOT EXISTS payroll_runs (
  id TEXT PRIMARY KEY, teacher_id TEXT NOT NULL REFERENCES users(id), period TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','finalized','paid','cancelled')),
  total_satang INTEGER NOT NULL DEFAULT 0, created_by TEXT NOT NULL REFERENCES users(id),
  created_at INTEGER NOT NULL, finalized_at INTEGER, paid_at INTEGER, payment_reference TEXT
);
CREATE TABLE IF NOT EXISTS payroll_lines (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES payroll_runs(id),
  time_entry_id TEXT NOT NULL UNIQUE REFERENCES time_entries(id),
  rate_rule_id TEXT REFERENCES rate_rules(id),
  minutes INTEGER NOT NULL, rate_satang_per_hour INTEGER,
  amount_satang INTEGER NOT NULL, pricing_type TEXT NOT NULL,
  override_reason TEXT, override_reference TEXT
);
CREATE TABLE IF NOT EXISTS payroll_adjustments (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES payroll_runs(id),
  amount_satang INTEGER NOT NULL, reason TEXT NOT NULL,
  created_by TEXT NOT NULL REFERENCES users(id), created_at INTEGER NOT NULL,
  CHECK(amount_satang != 0)
);
CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY, actor_id TEXT REFERENCES users(id), entity TEXT NOT NULL,
  entity_id TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_outbox (
  id TEXT PRIMARY KEY,
  audit_event_id TEXT NOT NULL UNIQUE REFERENCES audit_events(id),
  event_type TEXT NOT NULL,
  recipient_kind TEXT NOT NULL CHECK(recipient_kind IN ('admin','user')),
  recipient_user_id TEXT REFERENCES users(id),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK(status IN ('pending','sending','sent','dead')),
  attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
  next_attempt_at INTEGER NOT NULL,
  lease_token TEXT,
  lease_until INTEGER,
  provider_id TEXT,
  last_error_code TEXT,
  first_attempt_at INTEGER,
  created_at INTEGER NOT NULL,
  sent_at INTEGER,
  CHECK((recipient_kind='admin' AND recipient_user_id IS NULL) OR
        (recipient_kind='user' AND recipient_user_id IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS notification_outbox_ready
  ON notification_outbox(status,next_attempt_at,created_at);
CREATE TABLE IF NOT EXISTS app_migrations (
  key TEXT PRIMARY KEY, applied_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS code_counters (
  prefix TEXT PRIMARY KEY, next_number INTEGER NOT NULL CHECK(next_number > 0)
);
"""
SCHEMA += SHEET_SYNC_SCHEMA


def migrate_outbox_schema(conn: sqlite3.Connection) -> None:
    """Upgrade databases initialized before first-attempt tracking existed."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(notification_outbox)").fetchall()}
    if "first_attempt_at" not in columns:
        conn.execute("ALTER TABLE notification_outbox ADD COLUMN first_attempt_at INTEGER")
        conn.execute("UPDATE notification_outbox SET first_attempt_at=created_at WHERE attempts>0")


def seed_payment_account_and_sheet_queue(conn: sqlite3.Connection) -> None:
    """Keep owner-provided bank details and initially backfill all report tabs."""
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT OR IGNORE INTO payment_account"
        "(id,bank_code,bank_name,account_name,account_number,updated_at) "
        "VALUES(1,?,?,?,?,?)",
        (*DEFAULT_PAYMENT_ACCOUNT, now()),
    )
    current = now()
    conn.executemany(
        "INSERT OR IGNORE INTO sheet_sync_state(dataset,revision,updated_at) VALUES(?,1,?)",
        [(dataset, current) for dataset in SHEET_DATASETS],
    )
    conn.commit()


def next_public_code(conn: sqlite3.Connection, prefix: str) -> str:
    """Allocate a readable code inside the caller's BEGIN IMMEDIATE transaction."""
    conn.execute("INSERT OR IGNORE INTO code_counters(prefix,next_number) VALUES(?,1)", (prefix,))
    number = conn.execute("SELECT next_number FROM code_counters WHERE prefix=?", (prefix,)).fetchone()[0]
    conn.execute("UPDATE code_counters SET next_number=? WHERE prefix=?", (number + 1, prefix))
    return f"PP-{prefix}-{number:06d}"


def student_code_year(epoch: int | None = None) -> str:
    """The two-digit Gregorian registration year in the school's time zone."""
    year = datetime.fromtimestamp(now() if epoch is None else epoch, BANGKOK).year
    return f"{year % 100:02d}"


def next_student_code(conn: sqlite3.Connection, year: str | None = None) -> str:
    """Allocate YYNNN inside the caller's BEGIN IMMEDIATE transaction.

    The independent counter for each year is reconciled with existing codes,
    so a restored database or a legacy import cannot reuse a number.
    """
    year = student_code_year() if year is None else year
    if not isinstance(year, str) or not re.fullmatch(r"[0-9]{2}", year):
        raise ValueError("Student code year must have two ASCII digits")
    highest = conn.execute(
        "SELECT COALESCE(MAX(CAST(substr(public_code,3) AS INTEGER)),0) "
        "FROM users WHERE public_code GLOB ?",
        (f"{year}[0-9][0-9][0-9]",),
    ).fetchone()[0]
    counter = f"student:{year}"
    conn.execute(
        "INSERT OR IGNORE INTO code_counters(prefix,next_number) VALUES(?,?)",
        (counter, max(1, highest + 1)),
    )
    number = conn.execute(
        "SELECT next_number FROM code_counters WHERE prefix=?", (counter,)
    ).fetchone()[0]
    number = max(number, highest + 1)
    if number > 999:
        fail(409, "student_code_capacity_exhausted", "All student codes for this year have been used")
    conn.execute(
        "UPDATE code_counters SET next_number=? WHERE prefix=?", (number + 1, counter)
    )
    return f"{year}{number:03d}"


def migrate_student_codes(conn: sqlite3.Connection) -> bool:
    """Replace legacy visible codes once, preserving all internal foreign keys.

    Existing valid YYNNN codes remain stable. The audit log records old and
    new values; the dedicated student Sheets mirrors are refreshed afterward.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        if conn.execute(
            "SELECT 1 FROM app_migrations WHERE key=?", (STUDENT_CODE_MIGRATION,)
        ).fetchone():
            conn.commit()
            return False
        students = conn.execute(
            "SELECT id,public_code,created_at FROM users WHERE role='student' "
            "ORDER BY created_at,id"
        ).fetchall()
        changed_at = now()
        changed = False
        for student in students:
            old_code = student["public_code"]
            valid = (isinstance(old_code, str) and
                     re.fullmatch(r"[0-9]{5}", old_code) is not None and
                     old_code[2:] != "000")
            if valid:
                continue
            new_code = next_student_code(conn, student_code_year(student["created_at"]))
            conn.execute("UPDATE users SET public_code=? WHERE id=?", (new_code, student["id"]))
            conn.execute(
                "INSERT INTO audit_events(id,actor_id,entity,entity_id,action,details,created_at) "
                "VALUES(?,NULL,'user',?,'reissue_student_code',?,?)",
                (uid(), student["id"], json.dumps({"old_code": old_code, "new_code": new_code},
                                                    ensure_ascii=False, separators=(",", ":")), changed_at),
            )
            changed = True
        if changed:
            conn.executemany(
                "INSERT INTO sheet_sync_state(dataset,revision,updated_at,next_attempt_at) "
                "VALUES(?,1,?,0) ON CONFLICT(dataset) DO UPDATE SET "
                "revision=revision+1,updated_at=excluded.updated_at,next_attempt_at=0",
                [(dataset, changed_at) for dataset in ("students", "class_rosters", "enrollments")],
            )
        conn.execute(
            "INSERT INTO app_migrations(key,applied_at) VALUES(?,?)",
            (STUDENT_CODE_MIGRATION, changed_at),
        )
        conn.commit()
        return True
    except BaseException:
        conn.rollback()
        raise


def migrate_academic_schema(conn: sqlite3.Connection) -> None:
    """Upgrade existing SQLite/Turso data without reassigning historical IDs or invoices."""
    conn.execute("BEGIN IMMEDIATE")
    columns = lambda table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if "public_code" not in columns("users"):
        conn.execute("ALTER TABLE users ADD COLUMN public_code TEXT")
    if "course_code" not in columns("courses"):
        conn.execute("ALTER TABLE courses ADD COLUMN course_code TEXT")
    if "subject_code" not in columns("courses"):
        conn.execute("ALTER TABLE courses ADD COLUMN subject_code TEXT")
    if "room_name" not in columns("class_sessions"):
        conn.execute("ALTER TABLE class_sessions ADD COLUMN room_name TEXT")
    if "teacher_id" not in columns("class_sessions"):
        conn.execute("ALTER TABLE class_sessions ADD COLUMN teacher_id TEXT REFERENCES users(id)")
    conn.execute("CREATE TABLE IF NOT EXISTS code_counters(prefix TEXT PRIMARY KEY,next_number INTEGER NOT NULL CHECK(next_number>0))")
    for row in conn.execute("SELECT id,role FROM users WHERE role IN ('student','teacher') AND public_code IS NULL ORDER BY created_at,id").fetchall():
        prefix = "S" if row["role"] == "student" else "T"
        conn.execute("UPDATE users SET public_code=? WHERE id=?", (next_public_code(conn, prefix), row["id"]))
    for row in conn.execute("SELECT id,level,subject,price_satang,active,course_code,subject_code FROM courses ORDER BY created_at,id").fetchall():
        catalog = COURSE_CATALOG.get((row["level"], row["subject"]))
        subject_code = catalog[0] if catalog else "LEG"
        if row["course_code"] is None:
            conn.execute("UPDATE courses SET course_code=?,subject_code=? WHERE id=?",
                         (next_public_code(conn, subject_code), subject_code, row["id"]))
        elif row["subject_code"] is None:
            conn.execute("UPDATE courses SET subject_code=? WHERE id=?", (subject_code, row["id"]))
        if row["active"] and catalog is None:
            # Preserve historical enrollment/invoice values, but do not advertise
            # a subject that the shop no longer offers.
            conn.execute("UPDATE courses SET active=0 WHERE id=?", (row["id"],))
            conn.execute("INSERT INTO audit_events(id,actor_id,entity,entity_id,action,details,created_at) VALUES(?,NULL,'course',?,'deactivate_legacy_offer',?,?)",
                         (uid(), row["id"], json.dumps({"reason": "subject_not_offered"}, separators=(",", ":")), now()))
    conn.execute("UPDATE class_sessions SET teacher_id=(SELECT teacher_id FROM courses WHERE id=course_id) WHERE teacher_id IS NULL")
    conn.execute("UPDATE class_sessions SET room_name='ยังไม่ระบุ' WHERE room_name IS NULL OR trim(room_name)=''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_public_code_unique ON users(public_code)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS courses_course_code_unique ON courses(course_code)")
    conn.execute("CREATE INDEX IF NOT EXISTS class_sessions_teacher_time ON class_sessions(teacher_id,starts_at,ends_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS class_sessions_room_time ON class_sessions(room_name,starts_at,ends_at)")
    conn.commit()


def migrate_nullable_course_teachers(conn: sqlite3.Connection) -> None:
    """Allow an unassigned course without changing existing IDs or child records.

    SQLite cannot remove a NOT NULL constraint in place. Foreign keys must be
    disabled before the rebuild transaction, then checked against the copied
    table before committing and re-enabled before this connection is reused.
    """
    teacher_column = next(row for row in conn.execute("PRAGMA table_info(courses)")
                          if row[1] == "teacher_id")
    if not teacher_column[3]:
        return
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise RuntimeError("Course migration requires foreign key enforcement")
    conn.execute("PRAGMA foreign_keys=OFF")
    if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
        raise RuntimeError("Could not pause foreign key enforcement for course migration")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE courses_nullable_2026_10_21 ("
            "id TEXT PRIMARY KEY, course_code TEXT UNIQUE, subject_code TEXT, "
            "title TEXT NOT NULL, subject TEXT NOT NULL, "
            "level TEXT NOT NULL CHECK(level IN ('middle','high')), "
            "price_satang INTEGER NOT NULL CHECK(price_satang > 0), "
            "teacher_id TEXT REFERENCES users(id), "
            "capacity INTEGER NOT NULL CHECK(capacity BETWEEN 1 AND 100), "
            "active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), "
            "created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO courses_nullable_2026_10_21 "
            "(id,course_code,subject_code,title,subject,level,price_satang,teacher_id,capacity,active,created_at) "
            "SELECT id,course_code,subject_code,title,subject,level,price_satang,teacher_id,capacity,active,created_at "
            "FROM courses"
        )
        conn.execute("DROP TABLE courses")
        conn.execute("ALTER TABLE courses_nullable_2026_10_21 RENAME TO courses")
        conn.execute("CREATE UNIQUE INDEX courses_course_code_unique ON courses(course_code)")
        if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("Course migration would break a foreign key")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Foreign key enforcement was not restored")


def seed_initial_courses(conn: sqlite3.Connection) -> None:
    """Offer each requested subject once while preserving owner-created courses.

    The teacher, timetable and room are intentionally unset. The admin assigns
    them when organizing classes; students may enroll in the course beforehand.
    """
    conn.execute("BEGIN IMMEDIATE")
    if not conn.execute("SELECT 1 FROM app_migrations WHERE key=?", (COURSE_SEED_MIGRATION,)).fetchone():
        for (level, subject), (subject_code, subject_name) in COURSE_CATALOG.items():
            if conn.execute("SELECT 1 FROM courses WHERE level=? AND subject=? LIMIT 1",
                            (level, subject)).fetchone():
                continue
            course_id = uid()
            course_code = next_public_code(conn, subject_code)
            title = f"{subject_name} {'ม.ต้น' if level == 'middle' else 'ม.ปลาย'}"
            price_satang = 350_000 if level == "middle" else 450_000
            conn.execute(
                "INSERT INTO courses(id,course_code,subject_code,title,subject,level,price_satang,teacher_id,capacity,created_at) "
                "VALUES(?,?,?,?,?,?,?,NULL,?,?)",
                (course_id, course_code, subject_code, title, subject, level,
                 price_satang, INITIAL_COURSE_CAPACITY, now()),
            )
            conn.execute(
                "INSERT INTO audit_events(id,actor_id,entity,entity_id,action,details,created_at) "
                "VALUES(?,NULL,'course',?,'seed_initial_course',?,?)",
                (uid(), course_id, json.dumps({"course_code": course_code, "price_satang": price_satang},
                                              ensure_ascii=False, separators=(",", ":")), now()),
            )
        conn.execute("INSERT INTO app_migrations(key,applied_at) VALUES(?,?)", (COURSE_SEED_MIGRATION, now()))
    conn.commit()


def receipt_number(payment_id: str, paid_at: int) -> str:
    return "PP-RC-" + datetime.fromtimestamp(paid_at, BANGKOK).strftime("%Y%m%d") + "-" + payment_id[:12].upper()


def migrate_receipts(conn: sqlite3.Connection) -> None:
    """Create an acknowledgment for historical confirmed payments exactly once."""
    conn.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(receipts)")}
    for column in ("enrollment_id", "course_code", "course_title"):
        if column not in columns:
            conn.execute(f"ALTER TABLE receipts ADD COLUMN {column} TEXT")
    conn.execute(
        "UPDATE receipts SET enrollment_id=(SELECT i.enrollment_id FROM invoices i WHERE i.id=receipts.invoice_id),"
        "course_code=(SELECT c.course_code FROM invoices i JOIN enrollments e ON e.id=i.enrollment_id "
        "JOIN courses c ON c.id=e.course_id WHERE i.id=receipts.invoice_id),"
        "course_title=(SELECT c.title FROM invoices i JOIN enrollments e ON e.id=i.enrollment_id "
        "JOIN courses c ON c.id=e.course_id WHERE i.id=receipts.invoice_id) "
        "WHERE course_title IS NULL"
    )
    missing = conn.execute(
        "SELECT p.id,p.invoice_id,p.amount_satang,p.method,p.reference,p.recorded_at,"
        "i.invoice_number,i.student_id,i.description,i.enrollment_id,u.name student_name,"
        "c.course_code,c.title course_title "
        "FROM payments p JOIN invoices i ON i.id=p.invoice_id "
        "LEFT JOIN enrollments e ON e.id=i.enrollment_id LEFT JOIN courses c ON c.id=e.course_id "
        "JOIN users u ON u.id=i.student_id LEFT JOIN receipts r ON r.payment_id=p.id "
        "WHERE r.id IS NULL ORDER BY p.recorded_at,p.id"
    ).fetchall()
    issued_at = now()
    for payment in missing:
        conn.execute(
            "INSERT INTO receipts(id,payment_id,receipt_number,invoice_id,invoice_number,"
            "student_id,student_name,description,enrollment_id,course_code,course_title,"
            "amount_satang,method,reference,paid_at,issued_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid(), payment["id"], receipt_number(payment["id"], payment["recorded_at"]),
             payment["invoice_id"], payment["invoice_number"], payment["student_id"],
             payment["student_name"], payment["description"], payment["enrollment_id"],
             payment["course_code"], payment["course_title"], payment["amount_satang"],
             payment["method"], payment["reference"], payment["recorded_at"], issued_at),
        )
    conn.commit()


def seed_initial_teachers(conn: sqlite3.Connection) -> None:
    """Add the owner's first three staff records once, without inventing login emails."""
    conn.execute("BEGIN IMMEDIATE")
    if not conn.execute("SELECT 1 FROM app_migrations WHERE key=?", (TEACHER_ROSTER_MIGRATION,)).fetchone():
        for name in INITIAL_TEACHERS:
            exists = conn.execute("SELECT 1 FROM users WHERE role='teacher' AND name=? LIMIT 1", (name,)).fetchone()
            if exists:
                continue
            teacher_id = uid()
            conn.execute(
                "INSERT INTO users(id,email,public_code,name,password_hash,role,status,must_change_password,created_at) "
                "VALUES(?,?,?,?,?,'teacher','disabled',1,?)",
                (teacher_id, pending_teacher_email(teacher_id), next_public_code(conn, "T"), name,
                 hash_password(secrets.token_urlsafe(32)), now()),
            )
            conn.execute("INSERT INTO audit_events(id,actor_id,entity,entity_id,action,details,created_at) "
                         "VALUES(?,NULL,'user',?,'seed_teacher_roster','{}',?)", (uid(), teacher_id, now()))
        conn.execute("INSERT INTO app_migrations(key,applied_at) VALUES(?,?)", (TEACHER_ROSTER_MIGRATION, now()))
    conn.commit()


def migrate_brand_codes(conn: sqlite3.Connection) -> None:
    """Rename visible school identifiers; foreign keys use stable internal IDs."""
    conn.execute("BEGIN IMMEDIATE")
    if not conn.execute("SELECT 1 FROM app_migrations WHERE key=?", (BRAND_CODES_MIGRATION,)).fetchone():
        conn.execute("UPDATE users SET public_code='PP-'||substr(public_code,4) WHERE public_code LIKE 'PH-%'")
        conn.execute("UPDATE courses SET course_code='PP-'||substr(course_code,4) WHERE course_code LIKE 'PH-%'")
        # Issued invoice numbers are accounting references. Keep historical
        # numbers stable so bank transfers and exported records still reconcile.
        conn.execute("INSERT INTO app_migrations(key,applied_at) VALUES(?,?)", (BRAND_CODES_MIGRATION, now()))
    conn.commit()


RATE_GRID_EFFECTIVE_FROM = "2026-10-19"
RATE_GRID_MIGRATION = "owner_rate_grid_2026_10_19"
RATE_GRID = [
    ("middle", 1, 3, 20000, "อนุมานจากกติกาเจ้าของร้าน: ม.ต้นต่ำกว่า ม.ปลาย 50 บาท/ชม."),
    ("middle", 4, 6, 25000, "อนุมานจากกติกาเจ้าของร้าน: เพิ่มช่วงละ 50 บาท/ชม."),
    ("middle", 7, 9, 30000, "อนุมานจากกติกาเจ้าของร้าน: เพิ่มช่วงละ 50 บาท/ชม."),
    ("middle", 10, 13, 35000, "พบในชีต ชั่วโมงการสอน H2/H7/H66/H70; H20/H38 เป็นยอดพิเศษต้องตรวจรายคาบ"),
    ("high", 1, 3, 25000, "พบในชีต ชั่วโมงการสอน ครูโรจน์ H6 และครูโจ้ G16; H86/H94 เป็นยอดพิเศษต้องตรวจรายคาบ"),
    ("high", 4, 6, 30000, "พบในชีต ชั่วโมงการสอน H12/H45"),
    ("high", 7, 9, 35000, "อนุมานจากกติกาเจ้าของร้าน: เพิ่มช่วงละ 50 บาท/ชม."),
    ("high", 10, 13, 40000, "อนุมานจากกติกาเจ้าของร้าน: เพิ่มช่วงละ 50 บาท/ชม."),
]


class RateLimiter:
    def __init__(self) -> None:
        self.events: dict[str, deque[int]] = defaultdict(deque)
        self.lock = threading.Lock()

    def check(self, key: str, limit: int, period: int, *, conn=None) -> None:
        current = now()
        with self.lock:
            queue = self.events[key]
            while queue and queue[0] <= current - period:
                queue.popleft()
            if len(queue) >= limit:
                fail(429, "rate_limited", "Too many attempts; try again later")
            queue.append(current)


class ClosingConnection(sqlite3.Connection):
    """Commit/rollback at context exit and release the per-request handle."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class PhysicsPlusApp:
    def __init__(self, db_path: Path, origin: str, static_dir: Path | None = None) -> None:
        self.db_path = Path(db_path)
        self.origin = origin.rstrip("/")
        parsed = urlsplit(self.origin)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path:
            raise ValueError("origin must be an HTTP(S) origin with no path")
        self.allowed_origins = {self.origin}
        if parsed.hostname in {"127.0.0.1", "localhost"} and parsed.scheme == "http":
            self.allowed_origins.add(f"http://localhost:{parsed.port or 80}")
            self.allowed_origins.add(f"http://127.0.0.1:{parsed.port or 80}")
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self.limiter = RateLimiter()
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None, factory=ClosingConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.db_path.exists():
            descriptor = os.open(self.db_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(descriptor)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            migrate_outbox_schema(conn)
            migrate_academic_schema(conn)
            migrate_student_codes(conn)
            migrate_nullable_course_teachers(conn)
            migrate_brand_codes(conn)
            seed_initial_teachers(conn)
            seed_initial_courses(conn)
            migrate_receipts(conn)
            seed_payment_account_and_sheet_queue(conn)
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM app_migrations WHERE key=?", (RATE_GRID_MIGRATION,)).fetchone():
                # The old, unconfirmed four-row sample is superseded by the owner's
                # complete schedule. Confirmed teacher-specific history is retained.
                conn.execute("DELETE FROM rate_rules WHERE state='pending_confirmation' AND teacher_id IS NULL AND source_note LIKE 'Inferred hourly rate%'")
                conn.executemany(
                    "INSERT INTO rate_rules(id,teacher_label,subject,level,min_students,max_students,rate_satang_per_hour,effective_from,state,source_note,confirmed_at) VALUES(?,'อัตรากลาง','all',?,?,?,?,?,'confirmed',?,?)",
                    [(uid(), level, low, high, amount, RATE_GRID_EFFECTIVE_FROM, note, now()) for level, low, high, amount, note in RATE_GRID],
                )
                conn.execute("INSERT INTO app_migrations(key,applied_at) VALUES(?,?)", (RATE_GRID_MIGRATION, now()))
                self.audit(conn, None, "rate_schedule", RATE_GRID_MIGRATION, "activate_owner_grid", {"effective_from": RATE_GRID_EFFECTIVE_FROM, "tiers": len(RATE_GRID)})
            conn.commit()

    def audit(self, conn: sqlite3.Connection, actor_id: str | None, entity: str, entity_id: str, action: str, details: dict | None = None) -> str:
        event_id = uid()
        current = now()
        conn.execute("INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)", (event_id, actor_id, entity, entity_id, action, json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")), current))
        if not (entity == "user" and action in {"login", "change_password", "reset_password", "bootstrap_admin", "update_admin_account"}):
            conn.executemany(
                "INSERT INTO sheet_sync_state(dataset,revision,updated_at,next_attempt_at) "
                "VALUES(?,1,?,0) ON CONFLICT(dataset) DO UPDATE SET "
                "revision=revision+1,updated_at=excluded.updated_at,"
                "next_attempt_at=CASE WHEN sheet_sync_state.last_error_code IS NULL "
                "THEN 0 ELSE sheet_sync_state.next_attempt_at END",
                [(dataset, current) for dataset in SHEET_EVENT_DATASETS.get(entity, ())],
            )
        return event_id

    def enqueue_notice(self, conn: sqlite3.Connection, audit_event_id: str, event_type: str,
                       recipient_user_id: str | None = None) -> None:
        """Queue a fixed, detail-free notice in the same business transaction.

        A unique audit event can create only one notice. Admin destination is
        resolved from the environment at dispatch, so missing email setup
        does not silently discard an update.
        """
        if recipient_user_id:
            recipient = conn.execute("SELECT id,role,email FROM users WHERE id=?", (recipient_user_id,)).fetchone()
            if recipient and is_pending_teacher(recipient):
                return
        current = now()
        conn.execute(
            "INSERT INTO notification_outbox"
            "(id,audit_event_id,event_type,recipient_kind,recipient_user_id,next_attempt_at,created_at) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT(audit_event_id) DO NOTHING",
            (uid(), audit_event_id, event_type, "user" if recipient_user_id else "admin",
             recipient_user_id, current, current),
        )

    def dispatch_outbox(self, send_email, *, limit: int = 2,
                        admin_email: str | None = None) -> dict[str, int]:
        """Lease committed notices, send outside DB transactions, then persist outcome.

        `send_email(address, event_id=id)` is injected so tests cannot contact
        the mail provider. The lease protects concurrent Vercel invocations.
        Resend can use the stable ID as an idempotency key; SMTP has no
        exactly-once guarantee.
        """
        limit = max(0, min(int(limit), 25))
        admin_email = admin_email.strip() if isinstance(admin_email, str) else ""
        if admin_email:
            try:
                admin_email = normalize_email(admin_email)
            except ApiError:
                admin_email = ""
        outcome = {"processed": 0, "sent": 0, "retrying": 0, "dead": 0}
        for _ in range(limit):
            current = now()
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                # Hobby Cron can run only daily. Retry transient failures for
                # seven days. Resend can dedupe the stable event ID for 24
                # hours; SMTP has no provider idempotency key. An uncertain
                # acknowledgement may yield a duplicate generic notice.
                expired = conn.execute(
                    "UPDATE notification_outbox SET status='dead',last_error_code='retry_expired',"
                    "lease_token=NULL,lease_until=NULL "
                    "WHERE (status='pending' OR (status='sending' AND lease_until<=?)) "
                    "AND first_attempt_at IS NOT NULL AND first_attempt_at<=? "
                    "AND COALESCE(last_error_code,'') NOT IN ('not_configured','invalid_configuration')",
                    (current, current - 7 * 24 * 3600),
                ).rowcount
                outcome["dead"] += max(0, expired)
                row = conn.execute(
                    "SELECT id,event_type,recipient_kind,recipient_user_id,attempts,first_attempt_at "
                    "FROM notification_outbox "
                    "WHERE next_attempt_at<=? AND "
                    "(status='pending' OR (status='sending' AND lease_until<=?)) "
                    "AND (recipient_kind='user' OR ?=1) "
                    "ORDER BY created_at,id LIMIT 1",
                    (current, current, bool(admin_email)),
                ).fetchone()
                if row is None:
                    conn.commit()
                    break
                lease_token = uid()
                conn.execute(
                    "UPDATE notification_outbox SET status='sending',lease_token=?,lease_until=?,"
                    "attempts=attempts+1,first_attempt_at=COALESCE(first_attempt_at,?) WHERE id=?",
                    (lease_token, current + 60, current, row["id"]),
                )
                conn.commit()

            outcome["processed"] += 1
            recipient = admin_email if row["recipient_kind"] == "admin" else None
            active = True
            if row["recipient_kind"] == "user":
                with self.connect() as conn:
                    target = conn.execute("SELECT email,status FROM users WHERE id=?",
                                          (row["recipient_user_id"],)).fetchone()
                if target is not None:
                    recipient = target["email"]
                    active = target["status"] == "active"
                else:
                    active = False
            if recipient and active:
                try:
                    if row["event_type"] == "class_cancelled":
                        result = send_email(recipient, event_id=row["id"], event_type="class_cancelled")
                    else:
                        result = send_email(recipient, event_id=row["id"])
                    sent = result.sent is True
                    code = "rate_limited" if result.status == 429 else str(result.code)[:80]
                    retryable = result.retryable is True
                    provider_id = result.provider_id if sent else None
                    retry_after = result.retry_after_seconds
                except Exception:
                    # Never log addresses, mail-provider errors, or credentials.
                    sent, code, retryable, provider_id, retry_after = False, "send_exception", True, None, None
            else:
                sent, code, retryable, provider_id, retry_after = False, "recipient_inactive", False, None, None

            attempts = row["attempts"] + 1
            current = now()
            first_attempt_at = row["first_attempt_at"] or current
            if sent:
                status, next_attempt = "sent", current
                outcome["sent"] += 1
            elif code in {"not_configured", "invalid_configuration"}:
                # Configuration can be fixed after deployment; keep the notice.
                status, next_attempt = "pending", current + 3600
                outcome["retrying"] += 1
            elif retryable and attempts < 8 and current - first_attempt_at < 7 * 24 * 3600:
                backoff = min(3600, 30 * (2 ** min(attempts - 1, 7)))
                status, next_attempt = "pending", current + max(backoff, int(retry_after or 0))
                outcome["retrying"] += 1
            else:
                status, next_attempt = "dead", current
                outcome["dead"] += 1
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE notification_outbox SET status=?,next_attempt_at=?,lease_token=NULL,"
                    "lease_until=NULL,provider_id=?,last_error_code=?,sent_at=?,"
                    "first_attempt_at=CASE WHEN ?=1 THEN NULL ELSE first_attempt_at END,"
                    "attempts=attempts-? "
                    "WHERE id=? AND status='sending' AND lease_token=?",
                    (status, next_attempt, provider_id,
                     None if sent else code, current if sent else None,
                     int(code in {"not_configured", "invalid_configuration"}),
                     int(code in {"not_configured", "invalid_configuration"}),
                     row["id"], lease_token),
                )
                conn.commit()
        return outcome

    def bootstrap_admin(self, email: str | None, name: str, password: str, username: str | None = None) -> None:
        email = normalize_email(email) if email else None
        username = normalize_username(username) if username else None
        if not email and not username:
            fail(400, "invalid_account", "An admin email or username is required")
        name = name.strip()
        if not name or len(name) > 120:
            fail(400, "invalid_name", "Name must be 1–120 characters")
        password = validate_password(password)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
                fail(409, "admin_exists", "Admin already exists; use an existing admin account")
            user_id = uid()
            conn.execute("INSERT INTO users(id,email,username,name,password_hash,role,must_change_password,created_at) VALUES(?,?,?,?,?,'admin',0,?)", (user_id, email, username, name, hash_password(password), now()))
            self.audit(conn, user_id, "user", user_id, "bootstrap_admin")
            conn.commit()

    def _session(self, conn: sqlite3.Connection, cookie_header: str) -> tuple[sqlite3.Row | None, sqlite3.Row | None, str | None]:
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
            token = cookie.get("pp_session")
        except Exception:
            token = None
        if token is None:
            return None, None, None
        raw = token.value
        if not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", raw):
            return None, None, None
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        result = conn.execute("SELECT s.*,u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.status='active'", (token_hash, now())).fetchone()
        return result, result, token_hash

    def _new_session(self, conn: sqlite3.Connection, user_id: str) -> tuple[str, str]:
        raw = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now(),))
        conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?)", (hashlib.sha256(raw.encode()).hexdigest(), user_id, csrf, now(), now() + SESSION_SECONDS))
        cookie = f"pp_session={raw}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_SECONDS}"
        if self.origin.startswith("https://"):
            cookie += "; Secure"
        return cookie, csrf

    def _clear_cookie(self) -> str:
        cookie = "pp_session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"
        if self.origin.startswith("https://"):
            cookie += "; Secure"
        return cookie

    def _auth_payload(self, user: sqlite3.Row, csrf: str) -> dict:
        return {"authenticated": True, "user": user_json(user), "csrf_token": csrf}

    def _require_role(self, user: sqlite3.Row, *roles: str) -> None:
        if user["role"] not in roles:
            fail(403, "forbidden", "This action is not available for your account")

    def _wage_liability(self, conn: sqlite3.Connection, teacher_id: str | None = None) -> dict:
        base = "SELECT t.id,t.session_id,t.teacher_id,t.payable_minutes,t.amount_satang,s.starts_at,pl.run_id payroll_run_id,p.status payroll_status FROM time_entries t JOIN class_sessions s ON s.id=t.session_id LEFT JOIN payroll_lines pl ON pl.time_entry_id=t.id LEFT JOIN payroll_runs p ON p.id=pl.run_id WHERE t.status IN ('approved','payroll_locked')"
        if teacher_id:
            entries = conn.execute(base + " AND t.teacher_id=? ORDER BY s.starts_at DESC", (teacher_id,)).fetchall()
            grouped = conn.execute("SELECT status,COALESCE(SUM(total_satang),0) total FROM payroll_runs WHERE teacher_id=? AND status IN ('draft','finalized') GROUP BY status", (teacher_id,)).fetchall()
        else:
            entries = conn.execute(base + " ORDER BY s.starts_at DESC").fetchall()
            grouped = conn.execute("SELECT status,COALESCE(SUM(total_satang),0) total FROM payroll_runs WHERE status IN ('draft','finalized') GROUP BY status").fetchall()
        totals = {r["status"]: r["total"] for r in grouped}
        unrun = sum(r["amount_satang"] for r in entries if r["payroll_run_id"] is None)
        draft, finalized = totals.get("draft", 0), totals.get("finalized", 0)
        due = unrun + draft + finalized
        return {"approved_unpaid_entries": [dict(r) for r in entries], "approved_unrun_satang": unrun, "draft_payroll_satang": draft, "finalized_payroll_satang": finalized, "approved_unpaid_satang": due, "total_wages_due_satang": due}

    def _check_origin(self, headers: dict[str, str]) -> None:
        origin = headers.get("origin", "")
        host = headers.get("host", "")
        if origin not in self.allowed_origins or urlsplit(origin).netloc != host:
            fail(403, "invalid_origin", "Request must come from this site")

    def _check_host(self, headers: dict[str, str]) -> None:
        host = headers.get("host", "")
        if host not in {urlsplit(x).netloc for x in self.allowed_origins}:
            fail(400, "invalid_host", "Host is not allowed")

    def static_auth(self, route: str, cookie_header: str, *, include_overview: bool = False) -> tuple[str | None, dict | None]:
        """Authorize protected HTML and optionally reuse that read for admin counts."""
        if route not in {"/dashboard.html", "/student", "/teacher", "/admin"}:
            return None, None
        with self.connect() as conn:
            session, user, _ = self._session(conn, cookie_header)
            if user is None:
                return "login", None
            role_route = {"/student": "student", "/teacher": "teacher", "/admin": "admin"}.get(route)
            if role_route and user["role"] != role_route:
                return "forbidden", None
            payload = self._auth_payload(user, session["csrf_token"])
            if include_overview and user["role"] == "admin" and not user["must_change_password"]:
                try:
                    payload["overview_counts"] = self._admin_overview(conn).body
                except Exception:
                    # Counts are optional; a transient reporting read must
                    # never prevent a valid user from opening the workspace.
                    pass
            return None, payload

    def static_access(self, route: str, cookie_header: str) -> str | None:
        """Return login/forbidden for protected HTML paths, otherwise None."""
        return self.static_auth(route, cookie_header)[0]

    def handle(self, method: str, target: str, data: dict | None, headers: dict[str, str], client_ip: str) -> Response:
        try:
            self._check_host(headers)
            path = urlsplit(target).path
            query = parse_qs(urlsplit(target).query)
            if not path.startswith("/api/"):
                fail(404, "not_found", "API endpoint not found")
            if method not in {"GET", "POST", "PUT"}:
                fail(405, "method_not_allowed", "Method is not allowed")
            if method != "GET":
                self._check_origin(headers)
                if not isinstance(data, dict):
                    fail(400, "invalid_json", "A JSON object is required")
            if method == "GET" and path == "/api/health":
                return Response(200, {"ok": True, "database": "sqlite"})

            with self.connect() as conn:
                session, user, token_hash = self._session(conn, headers.get("cookie", ""))
                if method == "GET" and path == "/api/me":
                    return Response(200, self._auth_payload(user, session["csrf_token"]) if user else {"authenticated": False})

                if path in {"/api/auth/register", "/api/auth/login"} and method == "POST":
                    self.limiter.check(f"ip:{client_ip}", 20, 15 * 60, conn=conn)
                    if path.endswith("register"):
                        identifier = normalize_email(required_str(data, "email", 254))
                    else:
                        raw_login = data.get("login", data.get("email"))
                        if not isinstance(raw_login, str) or not raw_login.strip():
                            fail(400, "invalid_input", "login is required")
                        identifier = normalize_email(raw_login) if "@" in raw_login else normalize_username(raw_login)
                    self.limiter.check(f"login:{identifier}", 10, 15 * 60, conn=conn)
                    conn.execute("BEGIN IMMEDIATE")
                    response = self._register(conn, data, identifier) if path.endswith("register") else self._login(conn, data, identifier)
                    conn.commit()
                    return response

                if user is None:
                    fail(401, "unauthenticated", "Sign in first")
                if path == "/api/admin/maintenance/initialize" and method == "POST":
                    self._require_role(user, "admin")
                    csrf = headers.get("x-csrf-token", "")
                    if not csrf or not hmac.compare_digest(csrf, session["csrf_token"]):
                        fail(403, "invalid_csrf", "Refresh the page and try again")
                    current = data.get("current_password")
                    if not isinstance(current, str) or not verify_password(current, user["password_hash"]):
                        fail(401, "invalid_credentials", "Current password is incorrect")
                    initialize = getattr(self, "initialize_remote", None)
                    if initialize is None:
                        fail(503, "migration_unavailable", "Remote migration is unavailable")
                    initialize()
                    conn.execute("BEGIN IMMEDIATE")
                    self.audit(conn, user["id"], "system", "database", "initialize_remote")
                    conn.commit()
                    return Response(200, {"ok": True, "schema_initialized": True})
                if method != "GET":
                    csrf = headers.get("x-csrf-token", "")
                    if not csrf or not hmac.compare_digest(csrf, session["csrf_token"]):
                        fail(403, "invalid_csrf", "Refresh the page and try again")
                    conn.execute("BEGIN IMMEDIATE")

                if path == "/api/auth/logout" and method == "POST":
                    conn.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
                    conn.commit()
                    return Response(200, {"ok": True}, {"Set-Cookie": self._clear_cookie()})
                if path == "/api/auth/change-password" and method == "POST":
                    response = self._change_password(conn, user, data)
                    conn.commit()
                    return response
                if user["must_change_password"]:
                    fail(403, "password_change_required", "Change the temporary password first")

                response = self._dispatch(conn, method, path, query, data or {}, user)
                if method != "GET":
                    conn.commit()
                return response
        except ApiError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except sqlite3.IntegrityError:
            return Response(409, {"error": {"code": "conflict", "message": "The requested change conflicts with existing data"}})

    def _register(self, conn: sqlite3.Connection, data: dict, email: str) -> Response:
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            fail(409, "account_exists", "An account with this email already exists")
        name = required_str(data, "name", 120)
        password = validate_password(data.get("password"))
        profile = student_profile_input(data)
        user_id = uid()
        created_at = now()
        public_code = next_student_code(conn, student_code_year(created_at))
        conn.execute("INSERT INTO users(id,email,public_code,name,password_hash,role,created_at) VALUES(?,?,?,?,?,?,?)", (user_id, email, public_code, name, hash_password(password), "student", created_at))
        conn.execute("INSERT INTO student_profiles(user_id,grade,school,guardian_name,guardian_phone,guardian_relationship,updated_at) VALUES(?,?,?,?,?,?,?)", (user_id, *profile.values(), created_at))
        cookie, csrf = self._new_session(conn, user_id)
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        event_id = self.audit(conn, user_id, "user", user_id, "register")
        self.enqueue_notice(conn, event_id, "student_registered")
        return Response(201, self._auth_payload(user, csrf), {"Set-Cookie": cookie})

    def _login(self, conn: sqlite3.Connection, data: dict, identifier: str) -> Response:
        password = data.get("password")
        if not isinstance(password, str) or len(password) > 128:
            fail(401, "invalid_credentials", "Email or password is incorrect")
        if "@" in identifier:
            user = conn.execute("SELECT * FROM users WHERE email=?", (identifier,)).fetchone()
        else:
            user = conn.execute("SELECT * FROM users WHERE username=? AND role='admin'", (identifier,)).fetchone()
        if not verify_password(password, user["password_hash"] if user else DUMMY_PASSWORD_HASH) or user is None or user["status"] != "active":
            fail(401, "invalid_credentials", "Email or password is incorrect")
        cookie, csrf = self._new_session(conn, user["id"])
        self.audit(conn, user["id"], "user", user["id"], "login")
        return Response(200, self._auth_payload(user, csrf), {"Set-Cookie": cookie})

    def _change_password(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict) -> Response:
        current = data.get("current_password")
        if not isinstance(current, str) or not verify_password(current, user["password_hash"]):
            fail(401, "invalid_credentials", "Current password is incorrect")
        new_password = validate_password(data.get("new_password"))
        if hmac.compare_digest(current, new_password):
            fail(400, "weak_password", "New password must differ from the current password")
        conn.execute("UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?", (hash_password(new_password), user["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user["id"],))
        event_id = self.audit(conn, user["id"], "user", user["id"], "change_password")
        if user["email"] and user["status"] == "active":
            self.enqueue_notice(conn, event_id, "account_password_changed", user["id"])
        return Response(200, {"ok": True, "signed_out": True}, {"Set-Cookie": self._clear_cookie()})

    def _dispatch(self, conn: sqlite3.Connection, method: str, path: str, query: dict, data: dict, user: sqlite3.Row) -> Response:
        if method == "GET" and path == "/api/payment-account":
            if user["role"] not in {"student", "admin"}:
                fail(403, "forbidden", "Payment details are unavailable for this account")
            return Response(200, {"payment_account": self._payment_account(conn)})
        if method == "GET" and path == "/api/courses":
            rows = conn.execute("SELECT c.id,c.course_code,c.subject_code,c.title,c.subject,c.level,c.price_satang,c.capacity,u.name teacher_name,u.public_code teacher_code,(SELECT COUNT(*) FROM enrollments e WHERE e.course_id=c.id AND e.status='active') enrolled_count FROM courses c LEFT JOIN users u ON u.id=c.teacher_id WHERE c.active=1 ORDER BY c.created_at DESC").fetchall()
            return Response(200, {"courses": [dict(row) for row in rows]})
        if method == "GET" and path == "/api/student/dashboard":
            self._require_role(user, "student")
            return self._student_dashboard(conn, user)
        if method == "GET" and path == "/api/student/classes":
            self._require_role(user, "student")
            return Response(200, {"classes": self._class_list(conn, student_id=user["id"])})
        if method == "POST" and path == "/api/student/enrollments":
            self._require_role(user, "student")
            return self._student_enroll(conn, user, data)
        if method == "POST" and path == "/api/student/payment-claims":
            self._require_role(user, "student")
            return self._payment_claim(conn, user, data)
        receipt_match = re.fullmatch(r"/api/receipts/([0-9a-f]{32})", path)
        if method == "GET" and receipt_match:
            return self._receipt(conn, user, receipt_match.group(1))
        if method == "GET" and path == "/api/teacher/dashboard":
            self._require_role(user, "teacher")
            return self._teacher_dashboard(conn, user)
        if method == "GET" and path == "/api/teacher/classes":
            self._require_role(user, "teacher")
            return Response(200, {"classes": self._class_list(conn, teacher_id=user["id"])})
        teacher_cancel_match = re.fullmatch(r"/api/teacher/classes/([0-9a-f]{32})/cancel", path)
        if method == "POST" and teacher_cancel_match:
            self._require_role(user, "teacher")
            return self._teacher_cancel_class(conn, user, teacher_cancel_match.group(1), data)
        if method == "GET" and path == "/api/teacher/payroll":
            self._require_role(user, "teacher")
            rows = conn.execute("SELECT id,period,status,total_satang,finalized_at,paid_at FROM payroll_runs WHERE teacher_id=? AND status IN ('draft','finalized','paid') ORDER BY period DESC", (user["id"],)).fetchall()
            return Response(200, {"payroll_runs": [dict(row) for row in rows], **self._wage_liability(conn, user["id"])})
        teacher_run_match = re.fullmatch(r"/api/teacher/payroll-runs/([0-9a-f]{32})", path)
        if method == "GET" and teacher_run_match:
            self._require_role(user, "teacher")
            return self._payroll_detail(conn, teacher_run_match.group(1), user["id"])
        if method == "POST" and path == "/api/teacher/time-entries":
            self._require_role(user, "teacher")
            return self._submit_time(conn, user, data)
        roster_match = re.fullmatch(r"/api/(teacher|admin)/sessions/([0-9a-f]{32})/roster", path)
        if method == "GET" and roster_match:
            self._require_role(user, roster_match.group(1))
            return self._session_roster(conn, user, roster_match.group(2))
        attendance_match = re.fullmatch(r"/api/(teacher|admin)/sessions/([0-9a-f]{32})/attendance", path)
        if method == "PUT" and attendance_match:
            self._require_role(user, attendance_match.group(1))
            return self._set_attendance(conn, user, attendance_match.group(2), data)
        self._require_role(user, "admin")
        if method == "GET" and path == "/api/admin/overview":
            return self._admin_overview(conn)
        if method == "GET" and path == "/api/admin/dashboard":
            return self._admin_dashboard(conn, compact=query.get("compact") == ["1"])
        if method == "GET" and path == "/api/admin/payment-alerts":
            return self._admin_payment_alerts(conn)
        if method == "GET" and path == "/api/admin/notification-status":
            return Response(200, self._notification_status(conn))
        if method == "GET" and path == "/api/admin/sheet-sync/status":
            return Response(200, self._sheet_sync_status(conn))
        if method == "POST" and path == "/api/admin/sheet-sync/retry":
            self._queue_all_sheet_datasets(conn)
            return Response(200, {"queued": True, **self._sheet_sync_status(conn)})
        if method == "PUT" and path == "/api/admin/payment-account":
            return self._update_payment_account(conn, user, data)
        if method == "GET" and path == "/api/admin/users":
            return Response(200, {"users": self._admin_users(conn)})
        if method == "GET" and path == "/api/admin/academic":
            return self._admin_academic(conn)
        if method == "POST" and path == "/api/admin/account":
            return self._update_admin_account(conn, user, data)
        report_match = re.fullmatch(r"/api/admin/reports/(students|teachers|courses|classes|class_rosters|enrollments|invoices|payments|receipts|claims|teaching|payroll|rates)", path)
        if method == "GET" and report_match:
            return self._report(conn, report_match.group(1), query)
        if method == "POST" and path == "/api/admin/users":
            return self._create_user(conn, user, data, allowed_roles={"teacher", "student"})
        user_edit_match = re.fullmatch(r"/api/admin/users/([0-9a-f]{32})", path)
        if method == "PUT" and user_edit_match:
            return self._edit_user(conn, user, user_edit_match.group(1), data)
        profile_match = re.fullmatch(r"/api/admin/students/([0-9a-f]{32})/profile", path)
        if profile_match and method in {"GET", "PUT"}:
            return self._admin_student_profile(conn, user, profile_match.group(1), data if method == "PUT" else None)
        if method == "GET" and path == "/api/admin/rates":
            rows = conn.execute("SELECT * FROM rate_rules ORDER BY teacher_label,level,subject,min_students").fetchall()
            return Response(200, {"rates": [dict(row) for row in rows], "notice": "Owner rate grid is active from 2026-10-19; teacher-specific rules override it; exceptional sessions require a documented override"})
        if method == "POST" and path == "/api/admin/rates":
            return self._create_rate(conn, user, data)
        if method == "POST" and path == "/api/admin/staff":
            return self._create_staff(conn, user, data)
        activate_staff_match = re.fullmatch(r"/api/admin/staff/([0-9a-f]{32})/activate", path)
        if method == "POST" and activate_staff_match:
            return self._activate_staff(conn, user, activate_staff_match.group(1), data)
        account_match = re.fullmatch(r"/api/admin/users/([0-9a-f]{32})/(reset-password|disable|enable)", path)
        if method == "POST" and account_match:
            return self._manage_user(conn, user, account_match.group(1), account_match.group(2))
        if method == "POST" and path == "/api/admin/courses":
            return self._create_course(conn, user, data)
        course_edit_match = re.fullmatch(r"/api/admin/courses/([0-9a-f]{32})", path)
        if method == "PUT" and course_edit_match:
            return self._edit_course(conn, user, course_edit_match.group(1), data)
        if method == "POST" and path == "/api/admin/enrollments":
            return self._admin_enroll(conn, user, data)
        enrollment_action_match = re.fullmatch(r"/api/admin/enrollments/([0-9a-f]{32})/(cancel|restore)", path)
        if method == "POST" and enrollment_action_match:
            return self._manage_enrollment(conn, user, enrollment_action_match.group(1), enrollment_action_match.group(2))
        if method == "POST" and path == "/api/admin/classes":
            return self._create_class_session(conn, user, data)
        class_edit_match = re.fullmatch(r"/api/admin/classes/([0-9a-f]{32})", path)
        if method == "PUT" and class_edit_match:
            return self._edit_class_session(conn, user, class_edit_match.group(1), data)
        class_cancel_match = re.fullmatch(r"/api/admin/classes/([0-9a-f]{32})/cancel", path)
        if method == "POST" and class_cancel_match:
            return self._cancel_class_session(conn, user, class_cancel_match.group(1))
        if method == "POST" and path == "/api/admin/sessions":
            return self._create_class_session(conn, user, data)
        if method == "GET" and path == "/api/admin/payroll-runs":
            rows = conn.execute("SELECT p.id,p.period,p.status,p.total_satang,p.created_at,p.finalized_at,p.paid_at,p.payment_reference,u.name teacher_name FROM payroll_runs p JOIN users u ON u.id=p.teacher_id ORDER BY p.created_at DESC").fetchall()
            return Response(200, {"payroll_runs": [dict(row) for row in rows]})
        admin_run_match = re.fullmatch(r"/api/admin/payroll-runs/([0-9a-f]{32})", path)
        if method == "GET" and admin_run_match:
            return self._payroll_detail(conn, admin_run_match.group(1), None)
        if method == "POST" and path == "/api/admin/payroll-runs":
            return self._create_payroll(conn, user, data)
        if method == "POST" and path == "/api/admin/payments":
            return self._record_payment(conn, user, data, None)
        if method == "GET" and path == "/api/admin/invoices":
            return self._admin_invoices(conn)
        if method == "POST" and path == "/api/admin/invoices":
            return self._create_invoice(conn, user, data)
        invoice_match = re.fullmatch(r"/api/admin/invoices/([0-9a-f]{32})/(amount|due-date|void|restore)", path)
        if method == "POST" and invoice_match:
            return self._manage_invoice(conn, user, invoice_match.group(1), invoice_match.group(2), data)
        rate_match = re.fullmatch(r"/api/admin/rates/([0-9a-f]{32})/confirm", path)
        if method == "POST" and rate_match:
            return self._confirm_rate(conn, user, rate_match.group(1), data)
        rate_end_match = re.fullmatch(r"/api/admin/rates/([0-9a-f]{32})/end", path)
        if method == "POST" and rate_end_match:
            return self._end_rate(conn, user, rate_end_match.group(1), data)
        approve_match = re.fullmatch(r"/api/admin/time-entries/([0-9a-f]{32})/approve", path)
        if method == "POST" and approve_match:
            return self._approve_time(conn, user, approve_match.group(1))
        override_match = re.fullmatch(r"/api/admin/time-entries/([0-9a-f]{32})/approve-override", path)
        if method == "POST" and override_match:
            return self._approve_override(conn, user, override_match.group(1), data)
        reject_match = re.fullmatch(r"/api/admin/time-entries/([0-9a-f]{32})/reject", path)
        if method == "POST" and reject_match:
            return self._reject_time(conn, user, reject_match.group(1), data)
        claim_match = re.fullmatch(r"/api/admin/payment-claims/([0-9a-f]{32})/(approve|reject)", path)
        if method == "POST" and claim_match:
            return self._review_claim(conn, user, claim_match.group(1), claim_match.group(2))
        run_match = re.fullmatch(r"/api/admin/payroll-runs/([0-9a-f]{32})/(adjustments|cancel|finalize|mark-paid)", path)
        if method == "POST" and run_match:
            return self._payroll_action(conn, user, run_match.group(1), run_match.group(2), data)
        fail(404, "not_found", "API endpoint not found")

    def _student_dashboard(self, conn: sqlite3.Connection, user: sqlite3.Row) -> Response:
        rows = conn.execute("SELECT e.id enrollment_id,c.id course_id,c.title,c.subject,c.level,e.status,i.id invoice_id,i.invoice_number,i.amount_satang,i.status invoice_status,i.due_date,COALESCE((SELECT SUM(amount_satang) FROM payments p WHERE p.invoice_id=i.id),0) paid_satang FROM enrollments e JOIN courses c ON c.id=e.course_id LEFT JOIN invoices i ON i.enrollment_id=e.id WHERE e.student_id=? ORDER BY e.created_at DESC", (user["id"],)).fetchall()
        invoices = conn.execute("SELECT i.id,i.invoice_number,i.description,i.amount_satang,i.status,i.due_date,i.enrollment_id,COALESCE((SELECT SUM(amount_satang) FROM payments p WHERE p.invoice_id=i.id),0) paid_satang FROM invoices i WHERE i.student_id=? ORDER BY i.created_at DESC", (user["id"],)).fetchall()
        claims = conn.execute("SELECT pc.id,pc.invoice_id,pc.amount_satang,pc.reference,pc.status,pc.created_at FROM payment_claims pc WHERE pc.student_id=? ORDER BY pc.created_at DESC", (user["id"],)).fetchall()
        receipts = conn.execute("SELECT id,receipt_number,invoice_id,invoice_number,enrollment_id,course_code,course_title,description,amount_satang,method,reference,paid_at,issued_at FROM receipts WHERE student_id=? ORDER BY paid_at DESC,id DESC", (user["id"],)).fetchall()
        profile = conn.execute("SELECT grade,school,guardian_name,guardian_phone,guardian_relationship FROM student_profiles WHERE user_id=?", (user["id"],)).fetchone()
        return Response(200, {"enrollments": [dict(row) for row in rows], "invoices": [dict(row) for row in invoices], "payment_claims": [dict(row) for row in claims], "receipts": [dict(row) for row in receipts], "profile": dict(profile) if profile else None})

    def _receipt(self, conn: sqlite3.Connection, user: sqlite3.Row, receipt_id: str) -> Response:
        if user["role"] not in {"student", "admin"}:
            fail(403, "forbidden", "This document is not available for your account")
        receipt = conn.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
        if not receipt or (user["role"] == "student" and receipt["student_id"] != user["id"]):
            fail(404, "receipt_not_found", "Receipt not found")
        return Response(200, {"receipt": dict(receipt)})

    def _admin_student_profile(self, conn: sqlite3.Connection, actor: sqlite3.Row, student_id: str, data: dict | None) -> Response:
        student = conn.execute("SELECT id,name,email,status FROM users WHERE id=? AND role='student'", (student_id,)).fetchone()
        if not student:
            fail(404, "student_not_found", "Student not found")
        if data is not None:
            profile = student_profile_input(data)
            conn.execute("INSERT INTO student_profiles(user_id,grade,school,guardian_name,guardian_phone,guardian_relationship,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET grade=excluded.grade,school=excluded.school,guardian_name=excluded.guardian_name,guardian_phone=excluded.guardian_phone,guardian_relationship=excluded.guardian_relationship,updated_at=excluded.updated_at", (student_id, *profile.values(), now()))
            event_id = self.audit(conn, actor["id"], "student_profile", student_id, "update")
            if student["status"] == "active":
                self.enqueue_notice(conn, event_id, "student_profile_updated", student_id)
        row = conn.execute("SELECT grade,school,guardian_name,guardian_phone,guardian_relationship FROM student_profiles WHERE user_id=?", (student_id,)).fetchone()
        return Response(200, {"student": dict(student), "profile": dict(row) if row else None})

    def _student_enroll(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict) -> Response:
        if "amount_satang" in data or "price_satang" in data:
            fail(400, "amount_admin_only", "Students cannot set tuition amounts")
        course_id = required_str(data, "course_id", 32)
        profile = student_profile_input(data)
        course = conn.execute("SELECT * FROM courses WHERE id=? AND active=1", (course_id,)).fetchone()
        if not course:
            fail(404, "course_not_found", "Course is unavailable")
        if (profile["grade"] in {"M1", "M2", "M3"}) != (course["level"] == "middle"):
            fail(400, "grade_level_mismatch", "Student grade does not match the course level")
        enrolled = conn.execute("SELECT COUNT(*) FROM enrollments WHERE course_id=? AND status='active'", (course_id,)).fetchone()[0]
        if enrolled >= course["capacity"]:
            fail(409, "course_full", "Course is full")
        if conn.execute("SELECT 1 FROM enrollments WHERE course_id=? AND student_id=?", (course_id, user["id"])).fetchone():
            fail(409, "already_enrolled", "You are already enrolled")
        enrollment_id = uid()
        conn.execute("INSERT INTO student_profiles(user_id,grade,school,guardian_name,guardian_phone,guardian_relationship,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET grade=excluded.grade,school=excluded.school,guardian_name=excluded.guardian_name,guardian_phone=excluded.guardian_phone,guardian_relationship=excluded.guardian_relationship,updated_at=excluded.updated_at", (user["id"], *profile.values(), now()))
        conn.execute("INSERT INTO enrollments(id,course_id,student_id,created_at) VALUES(?,?,?,?)", (enrollment_id, course_id, user["id"], now()))
        event_id = self.audit(conn, user["id"], "enrollment", enrollment_id, "create", {"course_id": course_id})
        self.enqueue_notice(conn, event_id, "student_enrolled")
        return Response(201, {"enrollment_id": enrollment_id, "status": "awaiting_invoice"})

    def _payment_claim(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict) -> Response:
        invoice_id = required_str(data, "invoice_id", 32)
        invoice = conn.execute("SELECT * FROM invoices WHERE id=? AND student_id=?", (invoice_id, user["id"])).fetchone()
        if not invoice or invoice["status"] in {"paid", "void"}:
            fail(404, "invoice_not_found", "Invoice is unavailable")
        if "amount_satang" in data:
            fail(400, "amount_admin_only", "Only an administrator can set payment amounts")
        reference = required_str(data, "reference", 120)
        outstanding = invoice["amount_satang"] - conn.execute("SELECT COALESCE(SUM(amount_satang),0) FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
        if outstanding <= 0:
            fail(409, "invoice_paid", "This invoice has no remaining balance")
        if conn.execute("SELECT 1 FROM payment_claims WHERE invoice_id=? AND status='pending'", (invoice_id,)).fetchone():
            fail(409, "claim_pending", "A payment claim is already awaiting review")
        if conn.execute("SELECT 1 FROM payments WHERE reference=? COLLATE NOCASE", (reference,)).fetchone() or conn.execute("SELECT 1 FROM payment_claims WHERE reference=? COLLATE NOCASE AND status='pending'", (reference,)).fetchone():
            fail(409, "duplicate_payment", "This payment reference is already in use")
        claim_id = uid()
        conn.execute("INSERT INTO payment_claims(id,invoice_id,student_id,amount_satang,reference,created_at) VALUES(?,?,?,?,?,?)", (claim_id, invoice_id, user["id"], outstanding, reference, now()))
        event_id = self.audit(conn, user["id"], "payment_claim", claim_id, "submit", {"invoice_id": invoice_id})
        self.enqueue_notice(conn, event_id, "payment_claim_submitted")
        return Response(201, {"claim_id": claim_id, "status": "pending", "amount_satang": outstanding})

    def _teacher_dashboard(self, conn: sqlite3.Connection, user: sqlite3.Row) -> Response:
        rows = conn.execute("SELECT s.id,s.room_name,s.starts_at,s.ends_at,s.status,c.id course_id,c.course_code,c.title,c.subject,c.level,t.id time_entry_id,t.status time_status,t.actual_start,t.actual_end,t.amount_satang,t.rejection_reason,CASE WHEN s.status='scheduled' AND s.starts_at>? AND t.id IS NULL AND NOT EXISTS(SELECT 1 FROM attendance a WHERE a.session_id=s.id) THEN 1 ELSE 0 END can_cancel FROM class_sessions s JOIN courses c ON c.id=s.course_id LEFT JOIN time_entries t ON t.session_id=s.id WHERE s.teacher_id=? ORDER BY s.starts_at DESC LIMIT 200", (now(), user["id"])).fetchall()
        sessions = [dict(row) for row in rows]
        for session in sessions:
            session["can_cancel"] = bool(session["can_cancel"])
        self._add_cancellation_details(conn, sessions)
        return Response(200, {"sessions": sessions})

    def _submit_time(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict) -> Response:
        session_id = required_str(data, "session_id", 32)
        session = conn.execute("SELECT s.* FROM class_sessions s WHERE s.id=?", (session_id,)).fetchone()
        if not session or session["teacher_id"] != user["id"] or session["status"] != "scheduled":
            fail(404, "session_not_found", "Session is unavailable")
        start = parse_datetime(required_str(data, "actual_start", 50))
        end = parse_datetime(required_str(data, "actual_end", 50))
        duration = (end - start) // 60
        break_minutes = required_int(data, "break_minutes", 0, 480)
        if duration < 10 or duration > 480 or break_minutes >= duration or end > now() + 300:
            fail(400, "invalid_time", "Teaching time must be in the past, 10 minutes to 8 hours, with a shorter break")
        if abs(start - session["starts_at"]) > 24 * 3600:
            fail(400, "invalid_time", "Teaching time must be within 24 hours of the scheduled session")
        clash = conn.execute("SELECT 1 FROM time_entries WHERE teacher_id=? AND status IN ('submitted','approved','payroll_locked','paid') AND actual_start<? AND actual_end>? LIMIT 1", (user["id"], end, start)).fetchone()
        if clash:
            fail(409, "overlapping_time", "This time overlaps another teaching entry")
        existing = conn.execute("SELECT id,status FROM time_entries WHERE session_id=?", (session_id,)).fetchone()
        if existing and existing["status"] != "rejected":
            fail(409, "entry_exists", "This session already has an active time entry")
        if existing:
            entry_id = existing["id"]
            conn.execute("UPDATE time_entries SET actual_start=?,actual_end=?,break_minutes=?,status='submitted',payable_minutes=NULL,student_count=NULL,rate_rule_id=NULL,rate_satang_per_hour=NULL,amount_satang=NULL,pricing_type=NULL,override_reason=NULL,override_reference=NULL,submitted_at=?,reviewed_by=NULL,reviewed_at=NULL,rejection_reason=NULL WHERE id=?", (start, end, break_minutes, now(), entry_id))
        else:
            entry_id = uid()
            conn.execute("INSERT INTO time_entries(id,session_id,teacher_id,actual_start,actual_end,break_minutes,submitted_at) VALUES(?,?,?,?,?,?,?)", (entry_id, session_id, user["id"], start, end, break_minutes, now()))
        event_id = self.audit(conn, user["id"], "time_entry", entry_id, "resubmit" if existing else "submit", {"session_id": session_id})
        self.enqueue_notice(conn, event_id, "teaching_time_submitted")
        return Response(200 if existing else 201, {"time_entry_id": entry_id, "status": "submitted", "payable_minutes": duration - break_minutes})

    def _session_roster(self, conn: sqlite3.Connection, user: sqlite3.Row, session_id: str) -> Response:
        session = conn.execute("SELECT s.id,s.room_name,s.teacher_id,s.starts_at,s.ends_at,s.status,c.id course_id,c.course_code,c.title course_title FROM class_sessions s JOIN courses c ON c.id=s.course_id WHERE s.id=?", (session_id,)).fetchone()
        if not session:
            fail(404, "session_not_found", "Session is unavailable")
        if user["role"] == "teacher" and session["teacher_id"] != user["id"]:
            fail(403, "forbidden", "This session is assigned to another teacher")
        students = conn.execute("SELECT u.id,u.name,u.public_code student_code,a.status attendance_status FROM session_students ss JOIN users u ON u.id=ss.student_id LEFT JOIN attendance a ON a.session_id=ss.session_id AND a.student_id=ss.student_id WHERE ss.session_id=? ORDER BY u.name", (session_id,)).fetchall()
        return Response(200, {"session": {"id": session["id"], "room_name": session["room_name"], "teacher_id": session["teacher_id"], "course_id": session["course_id"], "course_code": session["course_code"], "course_title": session["course_title"], "starts_at": iso(session["starts_at"]), "ends_at": iso(session["ends_at"]), "status": session["status"]}, "students": [dict(r) for r in students], "attendance_complete": bool(students) and all(r["attendance_status"] in {"present", "absent"} for r in students)})

    def _set_attendance(self, conn: sqlite3.Connection, user: sqlite3.Row, session_id: str, data: dict) -> Response:
        session = conn.execute("SELECT s.* FROM class_sessions s WHERE s.id=?", (session_id,)).fetchone()
        if not session or session["status"] != "scheduled":
            fail(404, "session_not_found", "Session is unavailable")
        if user["role"] == "teacher" and session["teacher_id"] != user["id"]:
            fail(403, "forbidden", "This session is assigned to another teacher")
        if user["role"] == "teacher" and session["starts_at"] > now() + 15 * 60:
            fail(409, "attendance_too_early", "Attendance opens 15 minutes before class")
        entry = conn.execute("SELECT status FROM time_entries WHERE session_id=?", (session_id,)).fetchone()
        if entry and entry["status"] in {"approved", "payroll_locked", "paid"}:
            fail(409, "attendance_locked", "Attendance is locked after payroll approval")
        records = data.get("records")
        if not isinstance(records, list) or not 1 <= len(records) <= 100:
            fail(400, "invalid_input", "records must contain 1–100 students")
        seen = set()
        for record in records:
            if not isinstance(record, dict):
                fail(400, "invalid_input", "Each attendance record must be an object")
            student_id = required_str(record, "student_id", 32)
            status = required_str(record, "status", 10)
            if status not in {"present", "absent"} or student_id in seen:
                fail(400, "invalid_input", "Attendance status or duplicate student is invalid")
            seen.add(student_id)
            enrolled = conn.execute("SELECT 1 FROM session_students WHERE session_id=? AND student_id=?", (session_id, student_id)).fetchone()
            if not enrolled:
                fail(400, "not_enrolled", "Attendance student is not enrolled in this course")
            conn.execute("INSERT INTO attendance(session_id,student_id,status,recorded_by,recorded_at) VALUES(?,?,?,?,?) ON CONFLICT(session_id,student_id) DO UPDATE SET status=excluded.status,recorded_by=excluded.recorded_by,recorded_at=excluded.recorded_at", (session_id, student_id, status, user["id"], now()))
        self.audit(conn, user["id"], "attendance", session_id, "record", {"count": len(records)})
        return Response(200, {"ok": True, "recorded": len(records)})

    def _admin_invoices(self, conn: sqlite3.Connection) -> Response:
        invoices = conn.execute(
            "SELECT i.id,i.invoice_number,i.enrollment_id,i.student_id,u.name student_name,"
            "u.email student_email,c.title course_title,i.description,i.amount_satang,"
            "i.status,i.due_date,i.created_at,"
            "COALESCE((SELECT SUM(p.amount_satang) FROM payments p WHERE p.invoice_id=i.id),0) paid_satang "
            "FROM invoices i JOIN users u ON u.id=i.student_id "
            "LEFT JOIN enrollments e ON e.id=i.enrollment_id "
            "LEFT JOIN courses c ON c.id=e.course_id ORDER BY i.created_at DESC,i.id DESC LIMIT 500"
        ).fetchall()
        enrollments = conn.execute(
            "SELECT e.id enrollment_id,e.student_id,u.name student_name,u.email student_email,"
            "c.id course_id,c.title course_title,c.price_satang course_price_satang,e.created_at "
            "FROM enrollments e JOIN users u ON u.id=e.student_id "
            "JOIN courses c ON c.id=e.course_id LEFT JOIN invoices i ON i.enrollment_id=e.id "
            "WHERE e.status='active' AND i.id IS NULL "
            "ORDER BY e.created_at DESC,e.id DESC LIMIT 500"
        ).fetchall()
        receipts = conn.execute(
            "SELECT id,receipt_number,invoice_number,student_name,course_code,course_title,"
            "description,amount_satang,paid_at FROM receipts ORDER BY paid_at DESC,id DESC LIMIT 200"
        ).fetchall()
        return Response(200, {"invoices": [dict(row) for row in invoices],
                              "enrollments": [dict(row) for row in enrollments],
                              "receipts": [dict(row) for row in receipts]})

    def _create_invoice(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict) -> Response:
        enrollment_id = required_str(data, "enrollment_id", 32)
        amount = required_int(data, "amount_satang", 1, 100_000_000_00)
        due_date = required_str(data, "due_date", 10)
        if not DATE_RE.fullmatch(due_date):
            fail(400, "invalid_due_date", "Use a valid YYYY-MM-DD due date")
        try:
            datetime.strptime(due_date, "%Y-%m-%d")
        except ValueError:
            fail(400, "invalid_due_date", "Use a valid YYYY-MM-DD due date")
        enrollment = conn.execute(
            "SELECT e.id,e.student_id,c.title FROM enrollments e "
            "JOIN courses c ON c.id=e.course_id WHERE e.id=? AND e.status='active'",
            (enrollment_id,),
        ).fetchone()
        if not enrollment:
            fail(404, "enrollment_not_found", "Active enrollment not found")
        if conn.execute("SELECT 1 FROM invoices WHERE enrollment_id=?", (enrollment_id,)).fetchone():
            fail(409, "invoice_exists", "This enrollment already has an invoice")
        description = optional_str(data, "description", 250) or enrollment["title"]
        if any(ord(character) < 32 or ord(character) == 127 for character in description):
            fail(400, "invalid_input", "Control characters are not allowed")
        invoice_id = uid()
        invoice_number = "PP-" + datetime.now(BANGKOK).strftime("%Y%m%d") + "-" + invoice_id[:8].upper()
        conn.execute(
            "INSERT INTO invoices(id,invoice_number,enrollment_id,student_id,description,amount_satang,due_date,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (invoice_id, invoice_number, enrollment_id, enrollment["student_id"], description, amount, due_date, now()),
        )
        event_id = self.audit(conn, user["id"], "invoice", invoice_id, "create", {"enrollment_id": enrollment_id, "amount_satang": amount, "due_date": due_date})
        self.enqueue_notice(conn, event_id, "invoice_created", enrollment["student_id"])
        return Response(201, {"invoice_id": invoice_id, "invoice_number": invoice_number, "enrollment_id": enrollment_id, "amount_satang": amount, "due_date": due_date, "status": "unpaid"})

    def _manage_invoice(self, conn: sqlite3.Connection, user: sqlite3.Row, invoice_id: str, action: str, data: dict) -> Response:
        invoice = conn.execute("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if not invoice:
            fail(404, "invoice_not_found", "Invoice not found")
        if action == "restore":
            if invoice["status"] != "void" or conn.execute("SELECT 1 FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone():
                fail(409, "invoice_locked", "Only a void invoice without payments can be restored")
            conn.execute("UPDATE invoices SET status='unpaid' WHERE id=?", (invoice_id,))
            event_id = self.audit(conn, user["id"], "invoice", invoice_id, "restore")
            self.enqueue_notice(conn, event_id, "invoice_restored", invoice["student_id"])
            return Response(200, {"invoice_id": invoice_id, "status": "unpaid"})
        if action == "due-date":
            if invoice["status"] not in {"unpaid", "partial"}:
                fail(409, "invoice_locked", "This invoice can no longer be changed")
            due_date = required_str(data, "due_date", 10)
            if not DATE_RE.fullmatch(due_date):
                fail(400, "invalid_due_date", "Use a valid YYYY-MM-DD due date")
            try:
                datetime.strptime(due_date, "%Y-%m-%d")
            except ValueError:
                fail(400, "invalid_due_date", "Use a valid YYYY-MM-DD due date")
            conn.execute("UPDATE invoices SET due_date=? WHERE id=?", (due_date, invoice_id))
            event_id = self.audit(conn, user["id"], "invoice", invoice_id, "change_due_date", {"old_due_date": invoice["due_date"], "due_date": due_date})
            self.enqueue_notice(conn, event_id, "invoice_due_date_changed", invoice["student_id"])
            return Response(200, {"invoice_id": invoice_id, "due_date": due_date, "status": invoice["status"]})
        if invoice["status"] != "unpaid" or conn.execute("SELECT 1 FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone():
            fail(409, "invoice_locked", "Only an unpaid invoice without payments can be changed")
        if conn.execute("SELECT 1 FROM payment_claims WHERE invoice_id=? AND status='pending'", (invoice_id,)).fetchone():
            fail(409, "claim_pending", "Review the pending payment claim first")
        if action == "amount":
            amount = required_int(data, "amount_satang", 1, 100_000_000_00)
            conn.execute("UPDATE invoices SET amount_satang=? WHERE id=?", (amount, invoice_id))
            event_id = self.audit(conn, user["id"], "invoice", invoice_id, "change_amount", {"old_amount_satang": invoice["amount_satang"], "amount_satang": amount})
            self.enqueue_notice(conn, event_id, "invoice_amount_changed", invoice["student_id"])
            return Response(200, {"invoice_id": invoice_id, "amount_satang": amount, "status": "unpaid"})
        if action == "void":
            conn.execute("UPDATE invoices SET status='void' WHERE id=?", (invoice_id,))
            event_id = self.audit(conn, user["id"], "invoice", invoice_id, "void")
            self.enqueue_notice(conn, event_id, "invoice_voided", invoice["student_id"])
            return Response(200, {"invoice_id": invoice_id, "status": "void"})
        fail(404, "not_found", "Invoice action not found")

    def _admin_users(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT u.id,u.email,u.username,u.public_code,u.name,u.role,u.status,"
            "u.must_change_password,u.created_at,p.grade,p.school,p.guardian_name,"
            "p.guardian_phone,p.guardian_relationship "
            "FROM users u LEFT JOIN student_profiles p ON p.user_id=u.id ORDER BY u.role,u.name,u.id"
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            code = item.pop("public_code")
            item["student_code"] = code if item["role"] == "student" else None
            item["teacher_code"] = code if item["role"] == "teacher" else None
            item["login_enabled"] = item["status"] == "active"
            item["roster_pending"] = is_pending_teacher(item)
            item["email"] = public_email(item)
            result.append(item)
        return result

    def _class_list(self, conn: sqlite3.Connection, *, student_id: str | None = None,
                    teacher_id: str | None = None) -> list[dict]:
        criterion = ""
        args: tuple = ()
        if student_id:
            criterion = " WHERE EXISTS(SELECT 1 FROM session_students mine WHERE mine.session_id=s.id AND mine.student_id=?)"
            args = (student_id,)
        elif teacher_id:
            criterion = " WHERE s.teacher_id=?"
            args = (teacher_id,)
        rows = conn.execute(
            "SELECT s.id,s.room_name,s.course_id,c.title course_title,c.course_code,c.subject_code,"
            "s.teacher_id,u.name teacher_name,u.public_code teacher_code,s.starts_at,s.ends_at,s.status,"
            "(SELECT COUNT(*) FROM session_students ss WHERE ss.session_id=s.id) roster_count "
            "FROM class_sessions s JOIN courses c ON c.id=s.course_id JOIN users u ON u.id=s.teacher_id"
            + criterion + " ORDER BY s.starts_at DESC,s.id", args
        ).fetchall()
        classes = [dict(row) for row in rows]
        self._add_cancellation_details(conn, classes)
        if student_id:
            return classes
        roster_sql = (
            "SELECT ss.session_id,u.id,u.name,u.public_code student_code,a.status attendance_status "
            "FROM session_students ss JOIN users u ON u.id=ss.student_id "
            "LEFT JOIN attendance a ON a.session_id=ss.session_id AND a.student_id=ss.student_id "
        )
        if teacher_id:
            roster_sql += "JOIN class_sessions s ON s.id=ss.session_id WHERE s.teacher_id=? "
        roster_sql += "ORDER BY ss.session_id,u.name,u.id"
        rosters: dict[str, list[dict]] = defaultdict(list)
        for row in conn.execute(roster_sql, (teacher_id,) if teacher_id else ()).fetchall():
            item = dict(row)
            rosters[item.pop("session_id")].append(item)
        for item in classes:
            item["students"] = rosters.get(item["id"], [])
        return classes

    def _add_cancellation_details(self, conn: sqlite3.Connection, classes: list[dict]) -> None:
        """Expose the recorded reason without adding mutable columns to existing classes."""
        for row in classes:
            row["cancellation_reason"] = None
            row["cancelled_at"] = None
        cancelled_ids = [row["id"] for row in classes if row["status"] == "cancelled"]
        if not cancelled_ids:
            return
        details: dict[str, tuple[str | None, int]] = {}
        for offset in range(0, len(cancelled_ids), 300):
            chunk = cancelled_ids[offset:offset + 300]
            marks = ",".join("?" for _ in chunk)
            events = conn.execute(
                "SELECT entity_id,details,created_at FROM audit_events "
                "WHERE entity='class_session' AND action='cancel' "
                f"AND entity_id IN ({marks}) ORDER BY created_at DESC,id DESC",
                chunk,
            ).fetchall()
            for event in events:
                if event["entity_id"] in details:
                    continue
                try:
                    reason = json.loads(event["details"]).get("reason")
                except (TypeError, ValueError, AttributeError):
                    reason = None
                details[event["entity_id"]] = (
                    reason if isinstance(reason, str) else None, event["created_at"]
                )
        for row in classes:
            if row["id"] in details:
                row["cancellation_reason"], row["cancelled_at"] = details[row["id"]]

    def _admin_academic(self, conn: sqlite3.Connection) -> Response:
        courses = conn.execute(
            "SELECT c.id,c.course_code,c.subject_code,c.title,c.subject,c.level,c.price_satang,"
            "c.teacher_id,u.name teacher_name,u.public_code teacher_code,c.capacity,c.active,c.created_at,"
            "(SELECT COUNT(*) FROM enrollments e WHERE e.course_id=c.id AND e.status='active') enrolled_count "
            "FROM courses c LEFT JOIN users u ON u.id=c.teacher_id ORDER BY c.created_at DESC,c.id"
        ).fetchall()
        enrollments = conn.execute(
            "SELECT e.id,e.student_id,u.name student_name,u.public_code student_code,e.course_id,"
            "c.title course_title,c.course_code,e.status,e.created_at "
            "FROM enrollments e JOIN users u ON u.id=e.student_id JOIN courses c ON c.id=e.course_id "
            "ORDER BY e.created_at DESC,e.id"
        ).fetchall()
        catalog = [{"level": level, "subject": subject, "subject_code": code,
                    "title": f"{name} {'ม.ต้น' if level == 'middle' else 'ม.ปลาย'}"}
                   for (level, subject), (code, name) in COURSE_CATALOG.items()]
        return Response(200, {"catalog": catalog, "users": self._admin_users(conn),
                              "courses": [dict(row) for row in courses],
                              "classes": self._class_list(conn),
                              "enrollments": [dict(row) for row in enrollments]})

    @staticmethod
    def _admin_overview(conn: sqlite3.Connection) -> Response:
        """One read for the first screen; detailed records remain in existing APIs."""
        today = datetime.now(BANGKOK).date().isoformat()
        row = conn.execute(
            "WITH recent AS ("
            "SELECT i.id,i.amount_satang,i.status,i.due_date,"
            "COALESCE((SELECT SUM(p.amount_satang) FROM payments p WHERE p.invoice_id=i.id),0) paid_satang "
            "FROM invoices i ORDER BY i.created_at DESC,i.id DESC LIMIT 500) "
            "SELECT "
            "(SELECT COUNT(*) FROM recent) invoice_count,"
            "(SELECT COALESCE(SUM(CASE WHEN amount_satang>paid_satang THEN amount_satang-paid_satang ELSE 0 END),0) "
            "FROM recent WHERE status!='void') outstanding_satang,"
            "(SELECT COUNT(*) FROM recent WHERE status!='void' AND due_date<? AND amount_satang>paid_satang) overdue_count,"
            "(SELECT COUNT(*) FROM payment_claims WHERE status='pending') pending_claims,"
            "(SELECT COUNT(*) FROM time_entries WHERE status='submitted') pending_times,"
            "(SELECT MIN(COUNT(*),500) FROM enrollments e WHERE e.status='active' AND NOT EXISTS"
            "(SELECT 1 FROM invoices i WHERE i.enrollment_id=e.id)) unbilled_count,"
            "(SELECT COUNT(*) FROM notification_outbox WHERE status='dead') failed_notices",
            (today,),
        ).fetchone()
        return Response(200, dict(row))

    def _admin_dashboard(self, conn: sqlite3.Connection, *, compact: bool = False) -> Response:
        pending = conn.execute("SELECT t.id,t.actual_start,t.actual_end,t.break_minutes,u.name teacher_name,c.title course_title,c.subject,c.level,s.id session_id,(SELECT COUNT(*) FROM attendance a WHERE a.session_id=s.id AND a.status='present') student_count FROM time_entries t JOIN users u ON u.id=t.teacher_id JOIN class_sessions s ON s.id=t.session_id JOIN courses c ON c.id=s.course_id WHERE t.status='submitted' ORDER BY t.submitted_at DESC").fetchall()
        # The admin workspace loads the authoritative 500-row invoice list
        # from /api/admin/invoices. Keep the original 100-row result for the
        # full API, but avoid its duplicate remote read in compact mode.
        invoices = [] if compact else conn.execute("SELECT i.id,i.invoice_number,i.amount_satang,i.status,i.due_date,u.name student_name,COALESCE((SELECT SUM(p.amount_satang) FROM payments p WHERE p.invoice_id=i.id),0) paid_satang FROM invoices i JOIN users u ON u.id=i.student_id ORDER BY i.created_at DESC LIMIT 100").fetchall()
        claims = conn.execute("SELECT pc.id,pc.invoice_id,pc.amount_satang,pc.reference,pc.status,u.name student_name FROM payment_claims pc JOIN users u ON u.id=pc.student_id WHERE pc.status='pending' ORDER BY pc.created_at DESC").fetchall()
        shared = {"pending_time_entries": [dict(r) for r in pending], "invoices": [dict(r) for r in invoices],
                  "pending_payment_claims": [dict(r) for r in claims], "notifications": self._notification_status(conn)}
        if compact:
            # The admin workspace already reads its staff directory and wage
            # data from the academic/payroll endpoints. Avoid duplicate Turso
            # queries on the critical first paint without changing the full API.
            return Response(200, shared)
        staff = conn.execute("SELECT id,email,name,role,status,must_change_password FROM users WHERE role IN ('teacher','admin') ORDER BY role,name").fetchall()
        staff_rows = [{**dict(r), "email": public_email(r), "login_enabled": r["status"] == "active",
                       "roster_pending": is_pending_teacher(r)} for r in staff]
        return Response(200, {**shared, **self._wage_liability(conn), "staff": staff_rows,
                              "users": self._admin_users(conn)})

    @staticmethod
    def _admin_payment_alerts(conn: sqlite3.Connection) -> Response:
        """Small polling response for claims awaiting an administrator's review."""
        pending_count = conn.execute(
            "SELECT COUNT(*) FROM payment_claims WHERE status='pending'"
        ).fetchone()[0]
        claims = conn.execute(
            "SELECT pc.id,pc.invoice_id,i.invoice_number,pc.amount_satang,"
            "pc.reference,pc.status,pc.created_at,u.name student_name "
            "FROM payment_claims pc "
            "JOIN invoices i ON i.id=pc.invoice_id "
            "JOIN users u ON u.id=pc.student_id "
            "WHERE pc.status='pending' "
            "ORDER BY pc.created_at DESC,pc.id DESC LIMIT 50"
        ).fetchall()
        return Response(200, {"pending_payment_claims": [dict(row) for row in claims],
                              "pending_count": pending_count})

    def _notification_status(self, conn: sqlite3.Connection) -> dict:
        """Administrative delivery state without addresses or message content."""
        counts = {name: 0 for name in ("pending", "sending", "sent", "dead")}
        for row in conn.execute("SELECT status,COUNT(*) total FROM notification_outbox GROUP BY status"):
            counts[row["status"]] = row["total"]
        failed = conn.execute(
            "SELECT n.id,n.event_type,n.attempts,n.last_error_code,n.created_at,"
            "n.first_attempt_at,n.recipient_user_id,a.entity,a.entity_id "
            "FROM notification_outbox n JOIN audit_events a ON a.id=n.audit_event_id "
            "WHERE n.status='dead' ORDER BY n.created_at DESC,n.id DESC LIMIT 20"
        ).fetchall()
        return {"counts": counts, "failed": [dict(row) for row in failed]}

    @staticmethod
    def _payment_account(conn: sqlite3.Connection) -> dict:
        row = conn.execute(
            "SELECT bank_code,bank_name,account_name,account_number,updated_at "
            "FROM payment_account WHERE id=1"
        ).fetchone()
        if row is None:
            fail(503, "payment_account_unavailable", "Payment details are unavailable")
        return dict(row)

    def _update_payment_account(self, conn: sqlite3.Connection, actor: sqlite3.Row, data: dict) -> Response:
        expected = {"bank_code", "bank_name", "account_name", "account_number"}
        if set(data) != expected:
            fail(400, "invalid_input", "Complete all payment account fields")
        code = required_str(data, "bank_code", 20).lower()
        name = required_str(data, "bank_name", 100)
        holder = required_str(data, "account_name", 120)
        number = required_str(data, "account_number", 30)
        if not re.fullmatch(r"[a-z0-9]{2,20}", code) or not re.fullmatch(r"[0-9-]{8,30}", number) or not 8 <= len(number.replace("-", "")) <= 20:
            fail(400, "invalid_payment_account", "Check the bank code and account number")
        if any(ord(char) < 32 or ord(char) == 127 for char in name + holder):
            fail(400, "invalid_payment_account", "Control characters are not allowed")
        conn.execute(
            "UPDATE payment_account SET bank_code=?,bank_name=?,account_name=?,"
            "account_number=?,updated_by=?,updated_at=? WHERE id=1",
            (code, name, holder, number, actor["id"], now()),
        )
        self.audit(conn, actor["id"], "payment_account", "primary", "update")
        return Response(200, {"payment_account": self._payment_account(conn)})

    @staticmethod
    def _queue_all_sheet_datasets(conn: sqlite3.Connection) -> None:
        current = now()
        conn.executemany(
            "INSERT INTO sheet_sync_state(dataset,revision,updated_at,next_attempt_at) "
            "VALUES(?,1,?,0) ON CONFLICT(dataset) DO UPDATE SET "
            "revision=revision+1,updated_at=excluded.updated_at,next_attempt_at=0",
            [(dataset, current) for dataset in SHEET_DATASETS],
        )

    def _sheet_sync_status(self, conn: sqlite3.Connection) -> dict:
        from sheets_sync import sheet_configured, sheet_url

        row = conn.execute(
            "SELECT SUM(CASE WHEN revision>synced_revision THEN 1 ELSE 0 END) pending,"
            "MAX(synced_at) last_success_at FROM sheet_sync_state"
        ).fetchone()
        error = conn.execute(
            "SELECT last_error_code FROM sheet_sync_state WHERE last_error_code IS NOT NULL "
            "ORDER BY updated_at DESC,dataset DESC LIMIT 1"
        ).fetchone()
        return {
            "configured": sheet_configured(),
            "sheet_url": sheet_url(),
            "pending": int(row["pending"] or 0),
            "last_success_at": iso(row["last_success_at"]),
            "last_error_code": error["last_error_code"] if error else None,
        }

    def _report(self, conn: sqlite3.Connection, dataset: str, query: dict) -> Response:
        """Admin-only, paginated data for explicit CSV export to Google Sheets."""
        raw_page = query.get("page", ["1"])[0]
        if not isinstance(raw_page, str) or not re.fullmatch(r"[1-9]\d{0,3}", raw_page):
            fail(400, "invalid_page", "page must be between 1 and 9999")
        page = int(raw_page)
        sql = {
            "students": "SELECT u.public_code student_code,u.id,u.name,u.email,u.status,p.grade,p.school,p.guardian_name,p.guardian_phone,p.guardian_relationship,u.created_at FROM users u LEFT JOIN student_profiles p ON p.user_id=u.id WHERE u.role='student' ORDER BY u.created_at,u.id",
            "teachers": "SELECT id,public_code teacher_code,name,email,status,created_at FROM users WHERE role='teacher' ORDER BY created_at,id",
            "courses": "SELECT c.id,c.course_code,c.subject_code,c.title,c.subject,c.level,c.price_satang,c.capacity,c.active,u.name teacher_name,u.public_code teacher_code,c.created_at FROM courses c LEFT JOIN users u ON u.id=c.teacher_id ORDER BY c.created_at,c.id",
            "classes": "SELECT s.id,s.room_name,s.starts_at,s.ends_at,s.status,c.course_code,c.subject_code,c.title course_title,u.public_code teacher_code,u.name teacher_name,(SELECT COUNT(*) FROM session_students ss WHERE ss.session_id=s.id) roster_count,s.created_at FROM class_sessions s JOIN courses c ON c.id=s.course_id JOIN users u ON u.id=s.teacher_id ORDER BY s.starts_at,s.id",
            "class_rosters": "SELECT s.id class_id,s.room_name,c.course_code,c.title course_title,u.public_code student_code,u.name student_name,a.status attendance_status,s.starts_at,s.ends_at FROM session_students ss JOIN class_sessions s ON s.id=ss.session_id JOIN courses c ON c.id=s.course_id JOIN users u ON u.id=ss.student_id LEFT JOIN attendance a ON a.session_id=s.id AND a.student_id=u.id ORDER BY s.starts_at,s.id,u.name,u.id",
            "enrollments": "SELECT e.id,c.course_code,c.title course_title,u.public_code student_code,u.name student_name,u.email student_email,e.status,e.created_at FROM enrollments e JOIN courses c ON c.id=e.course_id JOIN users u ON u.id=e.student_id ORDER BY e.created_at,e.id",
            "invoices": "SELECT i.id,i.invoice_number,u.name student_name,u.email student_email,i.description,i.amount_satang,i.status,i.due_date,i.created_at FROM invoices i JOIN users u ON u.id=i.student_id ORDER BY i.created_at,i.id",
            "payments": "SELECT p.id,i.invoice_number,p.amount_satang,p.method,p.reference,p.recorded_at FROM payments p JOIN invoices i ON i.id=p.invoice_id ORDER BY p.recorded_at,p.id",
            "receipts": "SELECT r.id,r.receipt_number,r.invoice_number,r.student_name,r.enrollment_id,r.course_code,r.course_title,r.description,r.amount_satang,r.method,r.reference,r.paid_at,r.issued_at FROM receipts r ORDER BY r.paid_at,r.id",
            "claims": "SELECT pc.id,i.invoice_number,u.name student_name,pc.amount_satang,pc.reference,pc.status,pc.created_at,pc.reviewed_at FROM payment_claims pc JOIN invoices i ON i.id=pc.invoice_id JOIN users u ON u.id=pc.student_id ORDER BY pc.created_at,pc.id",
            "teaching": "SELECT t.id,u.name teacher_name,c.title course_title,t.actual_start,t.actual_end,t.break_minutes,t.payable_minutes,t.student_count,t.rate_satang_per_hour,t.amount_satang,t.status,t.submitted_at,t.reviewed_at FROM time_entries t JOIN users u ON u.id=t.teacher_id JOIN class_sessions s ON s.id=t.session_id JOIN courses c ON c.id=s.course_id ORDER BY t.submitted_at,t.id",
            "payroll": "SELECT p.id,u.name teacher_name,p.period,p.status,p.total_satang,p.finalized_at,p.paid_at,p.payment_reference,p.created_at FROM payroll_runs p JOIN users u ON u.id=p.teacher_id ORDER BY p.created_at,p.id",
            "rates": "SELECT id,teacher_label,subject,level,min_students,max_students,rate_satang_per_hour,effective_from,effective_to,state,source_note FROM rate_rules ORDER BY level,min_students,id",
        }[dataset]
        rows = conn.execute(sql + " LIMIT 1001 OFFSET ?", ((page - 1) * 1000,)).fetchall()
        exported = [dict(row) for row in rows[:1000]]
        if dataset == "classes":
            self._add_cancellation_details(conn, exported)
        if dataset == "teachers":
            for row in exported:
                if row["email"] == pending_teacher_email(row["id"]):
                    row["email"] = None
        return Response(200, {"dataset": dataset, "rows": exported, "next_page": page + 1 if len(rows) > 1000 else None})

    def _create_user(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict, allowed_roles: set[str]) -> Response:
        role = required_str(data, "role", 10)
        if role not in allowed_roles:
            fail(400, "invalid_role", "Role is not allowed on this endpoint")
        email = normalize_email(required_str(data, "email", 254))
        name = required_str(data, "name", 120)
        if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            fail(409, "account_exists", "An account with this email already exists")
        temporary_password = secrets.token_urlsafe(24)
        staff_id = uid()
        created_at = now()
        public_code = (next_student_code(conn, student_code_year(created_at)) if role == "student"
                       else next_public_code(conn, "T"))
        conn.execute("INSERT INTO users(id,email,public_code,name,password_hash,role,must_change_password,created_at) VALUES(?,?,?,?,?,?,1,?)", (staff_id, email, public_code, name, hash_password(temporary_password), role, created_at))
        event_id = self.audit(conn, user["id"], "user", staff_id, "create_user", {"role": role})
        self.enqueue_notice(conn, event_id, "account_created", staff_id)
        return Response(201, {"user": {"id": staff_id, "email": email, "name": name, "role": role, "student_code": public_code if role == "student" else None, "teacher_code": public_code if role == "teacher" else None}, "temporary_password": temporary_password, "must_change_password": True})

    def _update_admin_account(self, conn: sqlite3.Connection, actor: sqlite3.Row, data: dict) -> Response:
        if set(data) != {"current_password", "new_username", "new_password"}:
            fail(400, "invalid_input", "Current password, new username and new password are required")
        current = data.get("current_password")
        if not isinstance(current, str) or not verify_password(current, actor["password_hash"]):
            fail(401, "invalid_credentials", "Current password is incorrect")
        username = normalize_username(required_str(data, "new_username", 40))
        password = validate_password(data.get("new_password"))
        if username == actor["username"] and hmac.compare_digest(current, password):
            fail(400, "unchanged_account", "Choose a new username or password")
        if conn.execute("SELECT 1 FROM users WHERE username=? AND id!=?", (username, actor["id"])).fetchone():
            fail(409, "account_exists", "This username already exists")
        conn.execute("UPDATE users SET username=?,name=?,password_hash=?,must_change_password=0 WHERE id=?",
                     (username, "ผู้ดูแล Physics Plus", hash_password(password), actor["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (actor["id"],))
        self.audit(conn, actor["id"], "user", actor["id"], "update_admin_account",
                   {"old_username": actor["username"], "new_username": username})
        return Response(200, {"ok": True, "username": username, "sessions_revoked": True},
                        {"Set-Cookie": self._clear_cookie()})

    def _create_staff(self, conn: sqlite3.Connection, actor: sqlite3.Row, data: dict) -> Response:
        if "email" in data:
            return self._create_user(conn, actor, {**data, "role": "teacher"}, allowed_roles={"teacher"})
        if set(data) - {"name"}:
            fail(400, "invalid_input", "A staff roster entry only needs a name")
        name = required_str(data, "name", 120)
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            fail(400, "invalid_input", "Control characters are not allowed")
        if conn.execute("SELECT 1 FROM users WHERE role='teacher' AND name=?", (name,)).fetchone():
            fail(409, "staff_exists", "A teacher with this name already exists")
        teacher_id = uid()
        code = next_public_code(conn, "T")
        conn.execute(
            "INSERT INTO users(id,email,public_code,name,password_hash,role,status,must_change_password,created_at) "
            "VALUES(?,?,?,?,?,'teacher','disabled',1,?)",
            (teacher_id, pending_teacher_email(teacher_id), code, name,
             hash_password(secrets.token_urlsafe(32)), now()),
        )
        self.audit(conn, actor["id"], "user", teacher_id, "create_teacher_roster")
        return Response(201, {"user": {"id": teacher_id, "name": name, "role": "teacher",
                                        "teacher_code": code, "email": None, "login_enabled": False}})

    def _activate_staff(self, conn: sqlite3.Connection, actor: sqlite3.Row, teacher_id: str, data: dict) -> Response:
        if set(data) != {"email"}:
            fail(400, "invalid_input", "A real email is required to activate the teacher account")
        teacher = conn.execute("SELECT * FROM users WHERE id=? AND role='teacher'", (teacher_id,)).fetchone()
        if not teacher:
            fail(404, "teacher_not_found", "Teacher not found")
        if not is_pending_teacher(teacher):
            fail(409, "staff_already_activated", "This teacher already has a login account")
        email = normalize_email(required_str(data, "email", 254))
        if conn.execute("SELECT 1 FROM users WHERE email=? AND id!=?", (email, teacher_id)).fetchone():
            fail(409, "account_exists", "An account with this email already exists")
        temporary_password = secrets.token_urlsafe(24)
        conn.execute("UPDATE users SET email=?,password_hash=?,status='active',must_change_password=1 WHERE id=?",
                     (email, hash_password(temporary_password), teacher_id))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (teacher_id,))
        event_id = self.audit(conn, actor["id"], "user", teacher_id, "activate_teacher_account")
        self.enqueue_notice(conn, event_id, "account_created", teacher_id)
        return Response(200, {"user": {"id": teacher_id, "name": teacher["name"], "role": "teacher",
                                       "teacher_code": teacher["public_code"], "email": email,
                                       "login_enabled": True}, "temporary_password": temporary_password,
                              "must_change_password": True})

    def _edit_user(self, conn: sqlite3.Connection, actor: sqlite3.Row, target_id: str, data: dict) -> Response:
        target = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
        if not target:
            fail(404, "user_not_found", "Account not found")
        if target["role"] == "admin":
            fail(403, "protected_admin", "The sole admin account cannot be edited here")
        if not data or set(data) - {"name", "email"}:
            fail(400, "invalid_input", "Only name and email can be updated here")
        name = required_str(data, "name", 120) if "name" in data else target["name"]
        if is_pending_teacher(target) and "email" in data:
            fail(409, "staff_activation_required", "Activate this teacher account with a real email")
        email = normalize_email(required_str(data, "email", 254)) if "email" in data else target["email"]
        if any(ord(c) < 32 or ord(c) == 127 for c in name):
            fail(400, "invalid_input", "Control characters are not allowed")
        if conn.execute("SELECT 1 FROM users WHERE email=? AND id!=?", (email, target_id)).fetchone():
            fail(409, "account_exists", "An account with this email already exists")
        conn.execute("UPDATE users SET name=?,email=? WHERE id=?", (name, email, target_id))
        event_id = self.audit(conn, actor["id"], "user", target_id, "update_contact")
        if target["status"] == "active":
            self.enqueue_notice(conn, event_id, "account_updated", target_id)
        return Response(200, {"user_id": target_id, "name": name, "email": None if is_pending_teacher(target) else email,
                              "student_code": target["public_code"] if target["role"] == "student" else None,
                              "teacher_code": target["public_code"] if target["role"] == "teacher" else None})

    def _manage_user(self, conn: sqlite3.Connection, actor: sqlite3.Row, target_id: str, action: str) -> Response:
        target = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
        if not target:
            fail(404, "user_not_found", "Account not found")
        if target["id"] == actor["id"] or target["role"] == "admin":
            fail(403, "protected_admin", "Admin accounts cannot be changed through this endpoint")
        if is_pending_teacher(target):
            fail(409, "staff_activation_required", "Set a real email and activate this teacher account first")
        if action == "reset-password":
            temporary_password = secrets.token_urlsafe(24)
            conn.execute("UPDATE users SET password_hash=?,must_change_password=1 WHERE id=?", (hash_password(temporary_password), target_id))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
            event_id = self.audit(conn, actor["id"], "user", target_id, "reset_password")
            if target["status"] == "active":
                self.enqueue_notice(conn, event_id, "account_password_reset", target_id)
            return Response(200, {"user_id": target_id, "temporary_password": temporary_password, "must_change_password": True, "sessions_revoked": True})
        status = "disabled" if action == "disable" else "active"
        if target["status"] == status:
            fail(409, "status_unchanged", "Account already has this status")
        conn.execute("UPDATE users SET status=? WHERE id=?", (status, target_id))
        if status == "disabled":
            conn.execute("DELETE FROM sessions WHERE user_id=?", (target_id,))
        event_id = self.audit(conn, actor["id"], "user", target_id, action)
        if status == "active":
            self.enqueue_notice(conn, event_id, "account_enabled", target_id)
        return Response(200, {"user_id": target_id, "status": status, "sessions_revoked": status == "disabled"})

    def _create_course(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict) -> Response:
        subject = required_str(data, "subject", 40)
        level = required_str(data, "level", 10)
        catalog = COURSE_CATALOG.get((level, subject))
        if catalog is None:
            fail(400, "invalid_subject", "Choose a subject offered at this level")
        subject_code, subject_name = catalog
        price = required_int(data, "price_satang", 1, 100_000_000_00)
        title = f"{subject_name} {'ม.ต้น' if level == 'middle' else 'ม.ปลาย'}"
        teacher_id = data.get("teacher_id")
        if teacher_id is not None and (not isinstance(teacher_id, str) or
                                       not re.fullmatch(r"[0-9a-f]{32}", teacher_id)):
            fail(400, "invalid_teacher", "Choose a teacher or leave the course unassigned")
        capacity = required_int(data, "capacity", 1, 100)
        if teacher_id is not None and not eligible_teacher(conn, teacher_id):
            fail(400, "invalid_teacher", "Assign an active or pending roster teacher")
        course_id = uid()
        course_code = next_public_code(conn, subject_code)
        conn.execute("INSERT INTO courses(id,course_code,subject_code,title,subject,level,price_satang,teacher_id,capacity,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (course_id, course_code, subject_code, title, subject, level, price, teacher_id, capacity, now()))
        self.audit(conn, user["id"], "course", course_id, "create", {"teacher_id": teacher_id, "course_code": course_code})
        return Response(201, {"course_id": course_id, "course_code": course_code, "subject_code": subject_code, "title": title, "price_satang": price})

    def _edit_course(self, conn: sqlite3.Connection, actor: sqlite3.Row, course_id: str, data: dict) -> Response:
        course = conn.execute("SELECT * FROM courses WHERE id=?", (course_id,)).fetchone()
        if not course:
            fail(404, "course_not_found", "Course not found")
        if not data or set(data) - {"price_satang", "teacher_id", "capacity", "active"}:
            fail(400, "invalid_input", "Only price, teacher, capacity and active status can be updated")
        teacher_id = data.get("teacher_id", course["teacher_id"])
        if teacher_id is not None and (not isinstance(teacher_id, str) or
                                       not re.fullmatch(r"[0-9a-f]{32}", teacher_id) or
                                       not eligible_teacher(conn, teacher_id)):
            fail(400, "invalid_teacher", "Assign an active or pending roster teacher")
        price = required_int(data, "price_satang", 1, 100_000_000_00) if "price_satang" in data else course["price_satang"]
        capacity = required_int(data, "capacity", 1, 100) if "capacity" in data else course["capacity"]
        enrolled = conn.execute("SELECT COUNT(*) FROM enrollments WHERE course_id=? AND status='active'", (course_id,)).fetchone()[0]
        if capacity < enrolled:
            fail(409, "capacity_below_enrollment", "Capacity cannot be less than active enrollments")
        active = data.get("active", bool(course["active"]))
        if not isinstance(active, bool):
            fail(400, "invalid_input", "active must be true or false")
        catalog = COURSE_CATALOG.get((course["level"], course["subject"]))
        if active and catalog is None:
            fail(409, "legacy_course_subject", "This historical subject is no longer offered")
        conn.execute("UPDATE courses SET price_satang=?,teacher_id=?,capacity=?,active=? WHERE id=?", (price, teacher_id, capacity, int(active), course_id))
        self.audit(conn, actor["id"], "course", course_id, "update", {"old_price_satang": course["price_satang"], "price_satang": price, "teacher_id": teacher_id, "capacity": capacity, "active": active})
        return Response(200, {"course_id": course_id, "course_code": course["course_code"], "price_satang": price, "teacher_id": teacher_id, "capacity": capacity, "active": active})

    def _admin_enroll(self, conn: sqlite3.Connection, actor: sqlite3.Row, data: dict) -> Response:
        course_id = required_str(data, "course_id", 32)
        student_id = required_str(data, "student_id", 32)
        course = conn.execute("SELECT * FROM courses WHERE id=? AND active=1", (course_id,)).fetchone()
        student = conn.execute("SELECT u.id,u.status,p.grade FROM users u JOIN student_profiles p ON p.user_id=u.id WHERE u.id=? AND u.role='student'", (student_id,)).fetchone()
        if not course or not student or student["status"] != "active":
            fail(404, "enrollment_target_not_found", "Active student and course with a profile are required")
        if (student["grade"] in {"M1", "M2", "M3"}) != (course["level"] == "middle"):
            fail(400, "grade_level_mismatch", "Student grade does not match the course level")
        if conn.execute("SELECT 1 FROM enrollments WHERE course_id=? AND student_id=?", (course_id, student_id)).fetchone():
            fail(409, "already_enrolled", "Student already has an enrollment; restore it if cancelled")
        count = conn.execute("SELECT COUNT(*) FROM enrollments WHERE course_id=? AND status='active'", (course_id,)).fetchone()[0]
        if count >= course["capacity"]:
            fail(409, "course_full", "Course is full")
        enrollment_id = uid()
        conn.execute("INSERT INTO enrollments(id,course_id,student_id,created_at) VALUES(?,?,?,?)", (enrollment_id, course_id, student_id, now()))
        event_id = self.audit(conn, actor["id"], "enrollment", enrollment_id, "admin_create", {"course_id": course_id})
        self.enqueue_notice(conn, event_id, "student_enrollment_updated", student_id)
        return Response(201, {"enrollment_id": enrollment_id, "status": "awaiting_invoice"})

    def _manage_enrollment(self, conn: sqlite3.Connection, actor: sqlite3.Row, enrollment_id: str, action: str) -> Response:
        row = conn.execute("SELECT e.*,c.capacity,c.active course_active FROM enrollments e JOIN courses c ON c.id=e.course_id WHERE e.id=?", (enrollment_id,)).fetchone()
        if not row:
            fail(404, "enrollment_not_found", "Enrollment not found")
        desired = "cancelled" if action == "cancel" else "active"
        if row["status"] == desired:
            fail(409, "status_unchanged", "Enrollment already has this status")
        if action == "cancel":
            assigned = conn.execute("SELECT 1 FROM session_students ss JOIN class_sessions s ON s.id=ss.session_id WHERE ss.student_id=? AND s.course_id=? AND s.status='scheduled' AND s.starts_at>? LIMIT 1", (row["student_id"], row["course_id"], now())).fetchone()
            if assigned:
                fail(409, "class_roster_exists", "Remove the student from future class rosters first")
        else:
            if not row["course_active"]:
                fail(409, "course_inactive", "Activate the course before restoring enrollment")
            count = conn.execute("SELECT COUNT(*) FROM enrollments WHERE course_id=? AND status='active'", (row["course_id"],)).fetchone()[0]
            if count >= row["capacity"]:
                fail(409, "course_full", "Course is full")
        conn.execute("UPDATE enrollments SET status=? WHERE id=?", (desired, enrollment_id))
        event_id = self.audit(conn, actor["id"], "enrollment", enrollment_id, action)
        self.enqueue_notice(conn, event_id, "student_enrollment_updated", row["student_id"])
        return Response(200, {"enrollment_id": enrollment_id, "status": desired})

    def _create_class_session(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict) -> Response:
        course_id, room_name, teacher_id, start, end, student_ids = self._validated_class_input(conn, data, None)
        session_id = uid()
        conn.execute("INSERT INTO class_sessions(id,course_id,room_name,teacher_id,starts_at,ends_at,created_at) VALUES(?,?,?,?,?,?,?)",
                     (session_id, course_id, room_name, teacher_id, start, end, now()))
        conn.executemany("INSERT INTO session_students(session_id,student_id) VALUES(?,?)",
                         [(session_id, student_id) for student_id in student_ids])
        self.audit(conn, user["id"], "class_session", session_id, "create",
                   {"course_id": course_id, "room_name": room_name, "teacher_id": teacher_id, "roster_count": len(student_ids)})
        self._notify_class_members(conn, user["id"], session_id, "create", {teacher_id, *student_ids})
        return Response(201, {"session_id": session_id, "class_id": session_id, "room_name": room_name, "teacher_id": teacher_id, "roster_count": len(student_ids)})

    def _validated_class_input(self, conn: sqlite3.Connection, data: dict, existing_id: str | None) -> tuple:
        course_id = required_str(data, "course_id", 32)
        course = conn.execute("SELECT id,capacity,level FROM courses WHERE id=? AND active=1", (course_id,)).fetchone()
        if not course:
            fail(404, "course_not_found", "Active course is required")
        room_name = " ".join(required_str(data, "room_name", 80).split())
        if not room_name or any(ord(c) < 32 or ord(c) == 127 for c in room_name):
            fail(400, "invalid_room", "Enter a valid room name")
        teacher_id = required_str(data, "teacher_id", 32)
        if not eligible_teacher(conn, teacher_id):
            fail(400, "invalid_teacher", "Assign an active or pending roster teacher")
        start = parse_datetime(required_str(data, "starts_at", 50))
        end = parse_datetime(required_str(data, "ends_at", 50))
        if end - start < 10 * 60 or end - start > 8 * 3600:
            fail(400, "invalid_time", "Class must last between 10 minutes and 8 hours")
        student_ids = data.get("student_ids")
        if not isinstance(student_ids, list) or len(student_ids) > course["capacity"] or len(student_ids) > 13:
            fail(400, "invalid_roster", "Choose at most 13 students per class for the approved wage grid")
        if any(not isinstance(sid, str) or not re.fullmatch(r"[0-9a-f]{32}", sid) for sid in student_ids) or len(set(student_ids)) != len(student_ids):
            fail(400, "invalid_roster", "Student IDs must be unique and valid")
        if student_ids:
            marks = ",".join("?" for _ in student_ids)
            members = conn.execute(
                "SELECT e.student_id,p.grade FROM enrollments e JOIN users u ON u.id=e.student_id "
                "JOIN student_profiles p ON p.user_id=u.id "
                f"WHERE e.course_id=? AND e.status='active' AND u.status='active' AND e.student_id IN ({marks})",
                (course_id, *student_ids)
            ).fetchall()
            if len(members) != len(student_ids):
                fail(400, "roster_not_enrolled", "Every student must have an active enrollment in this course")
            if any((row["grade"] in {"M1", "M2", "M3"}) != (course["level"] == "middle") for row in members):
                fail(400, "grade_level_mismatch", "Student grade does not match the course level")
        conflict = conn.execute(
            "SELECT id FROM class_sessions WHERE status='scheduled' AND id!=? "
            "AND starts_at<? AND ends_at>? AND (teacher_id=? OR room_name=? COLLATE NOCASE) LIMIT 1",
            (existing_id or "", end, start, teacher_id, room_name)
        ).fetchone()
        if conflict:
            fail(409, "class_time_conflict", "Teacher or room is already booked at that time")
        if student_ids:
            marks = ",".join("?" for _ in student_ids)
            conflict = conn.execute(
                "SELECT 1 FROM class_sessions s JOIN session_students ss ON ss.session_id=s.id "
                "WHERE s.status='scheduled' AND s.id!=? AND s.starts_at<? AND s.ends_at>? "
                f"AND ss.student_id IN ({marks}) LIMIT 1",
                (existing_id or "", end, start, *student_ids)
            ).fetchone()
            if conflict:
                fail(409, "student_time_conflict", "A student is already assigned to another class at that time")
        return course_id, room_name, teacher_id, start, end, student_ids

    def _class_is_locked(self, conn: sqlite3.Connection, session_id: str) -> bool:
        return bool(
            conn.execute("SELECT 1 FROM time_entries WHERE session_id=? LIMIT 1", (session_id,)).fetchone()
            or conn.execute("SELECT 1 FROM attendance WHERE session_id=? LIMIT 1", (session_id,)).fetchone()
        )

    def _notify_class_members(self, conn: sqlite3.Connection, actor_id: str, session_id: str,
                              action: str, member_ids: set[str],
                              *, event_type: str = "class_schedule_updated") -> None:
        for member_id in sorted(member_ids):
            event_id = self.audit(conn, actor_id, "class_session", session_id, f"notify_{action}",
                                  {"recipient_user_id": member_id})
            self.enqueue_notice(conn, event_id, event_type, member_id)

    def _edit_class_session(self, conn: sqlite3.Connection, actor: sqlite3.Row, session_id: str, data: dict) -> Response:
        old = conn.execute("SELECT * FROM class_sessions WHERE id=?", (session_id,)).fetchone()
        if not old:
            fail(404, "class_not_found", "Class not found")
        if old["status"] != "scheduled" or self._class_is_locked(conn, session_id):
            fail(409, "class_locked", "A cancelled class or class with attendance/time cannot be edited")
        old_students = {row[0] for row in conn.execute("SELECT student_id FROM session_students WHERE session_id=?", (session_id,)).fetchall()}
        course_id, room_name, teacher_id, start, end, student_ids = self._validated_class_input(conn, data, session_id)
        conn.execute("UPDATE class_sessions SET course_id=?,room_name=?,teacher_id=?,starts_at=?,ends_at=? WHERE id=?",
                     (course_id, room_name, teacher_id, start, end, session_id))
        conn.execute("DELETE FROM session_students WHERE session_id=?", (session_id,))
        conn.executemany("INSERT INTO session_students(session_id,student_id) VALUES(?,?)",
                         [(session_id, student_id) for student_id in student_ids])
        self.audit(conn, actor["id"], "class_session", session_id, "update",
                   {"course_id": course_id, "room_name": room_name, "teacher_id": teacher_id,
                    "old_roster_count": len(old_students), "roster_count": len(student_ids)})
        self._notify_class_members(conn, actor["id"], session_id, "update",
                                   {old["teacher_id"], teacher_id, *old_students, *student_ids})
        return Response(200, {"session_id": session_id, "class_id": session_id, "status": "scheduled", "roster_count": len(student_ids)})

    def _cancel_class_session(self, conn: sqlite3.Connection, actor: sqlite3.Row, session_id: str) -> Response:
        row = conn.execute("SELECT * FROM class_sessions WHERE id=?", (session_id,)).fetchone()
        if not row:
            fail(404, "class_not_found", "Class not found")
        if row["status"] != "scheduled" or self._class_is_locked(conn, session_id):
            fail(409, "class_locked", "A cancelled class or class with attendance/time cannot be cancelled")
        students = {r[0] for r in conn.execute("SELECT student_id FROM session_students WHERE session_id=?", (session_id,)).fetchall()}
        conn.execute("UPDATE class_sessions SET status='cancelled' WHERE id=?", (session_id,))
        self.audit(conn, actor["id"], "class_session", session_id, "cancel")
        self._notify_class_members(conn, actor["id"], session_id, "cancel", {row["teacher_id"], *students})
        return Response(200, {"session_id": session_id, "class_id": session_id, "status": "cancelled"})

    def _teacher_cancel_class(self, conn: sqlite3.Connection, teacher: sqlite3.Row,
                              session_id: str, data: dict) -> Response:
        session = conn.execute("SELECT id,teacher_id,starts_at,status FROM class_sessions WHERE id=?",
                               (session_id,)).fetchone()
        if not session or session["teacher_id"] != teacher["id"]:
            fail(404, "class_not_found", "Class not found")
        if session["status"] != "scheduled" or session["starts_at"] <= now() or self._class_is_locked(conn, session_id):
            fail(409, "class_locked", "Only an upcoming class without attendance or teaching time can be cancelled")
        reason = " ".join(required_str(data, "reason", 300).split())
        if len(reason) < 3 or sum(char.isalnum() for char in reason) < 2:
            fail(400, "invalid_reason", "Enter a short, meaningful cancellation reason")
        students = {row[0] for row in conn.execute(
            "SELECT student_id FROM session_students WHERE session_id=?", (session_id,)
        ).fetchall()}
        conn.execute("UPDATE class_sessions SET status='cancelled' WHERE id=?", (session_id,))
        event_id = self.audit(conn, teacher["id"], "class_session", session_id, "cancel",
                              {"by_role": "teacher", "reason": reason, "roster_count": len(students)})
        cancelled_at = conn.execute("SELECT created_at FROM audit_events WHERE id=?", (event_id,)).fetchone()[0]
        self.enqueue_notice(conn, event_id, "class_cancelled")
        self._notify_class_members(conn, teacher["id"], session_id, "cancel", students,
                                   event_type="class_cancelled")
        return Response(200, {"session_id": session_id, "class_id": session_id,
                              "status": "cancelled", "cancellation_reason": reason,
                              "cancelled_at": cancelled_at})

    def _confirm_rate(self, conn: sqlite3.Connection, user: sqlite3.Row, rate_id: str, data: dict) -> Response:
        rule = conn.execute("SELECT * FROM rate_rules WHERE id=?", (rate_id,)).fetchone()
        if not rule:
            fail(404, "rate_not_found", "Rate rule does not exist")
        if rule["state"] != "pending_confirmation":
            fail(409, "rate_locked", "Rate rule has already been reviewed")
        teacher_id = required_str(data, "teacher_id", 32)
        teacher = conn.execute("SELECT * FROM users WHERE id=? AND role='teacher'", (teacher_id,)).fetchone()
        if not eligible_teacher(conn, teacher_id):
            fail(400, "invalid_teacher", "Select an active or pending roster teacher")
        mismatch_reason = None
        if teacher["name"].strip().casefold() != rule["teacher_label"].strip().casefold():
            if data.get("confirm_name_mismatch") is not True:
                fail(409, "teacher_label_mismatch", "Selected teacher name differs from the inferred rate label; review the mapping")
            mismatch_reason = required_str(data, "reason", 250)
        effective_from = required_str(data, "effective_from", 10)
        if not DATE_RE.fullmatch(effective_from):
            fail(400, "invalid_date", "Use YYYY-MM-DD")
        try:
            datetime.fromisoformat(effective_from)
        except ValueError:
            fail(400, "invalid_date", "Use a real calendar date")
        if self._rate_overlap(conn, teacher_id, rule["subject"], rule["level"], rule["min_students"], rule["max_students"], effective_from, None):
            fail(409, "overlapping_rate", "A confirmed rate already covers this student range and date")
        conn.execute("UPDATE rate_rules SET teacher_id=?,effective_from=?,state='confirmed',confirmed_by=?,confirmed_at=? WHERE id=?", (teacher_id, effective_from, user["id"], now(), rate_id))
        self.audit(conn, user["id"], "rate_rule", rate_id, "confirm", {"teacher_id": teacher_id, "effective_from": effective_from, "rate_satang_per_hour": rule["rate_satang_per_hour"], "name_mismatch_reason": mismatch_reason})
        return Response(200, {"rate_id": rate_id, "state": "confirmed"})

    def _rate_overlap(self, conn: sqlite3.Connection, teacher_id: str | None, subject: str, level: str, min_students: int, max_students: int, start: str, end: str | None, exclude_id: str | None = None) -> bool:
        if teacher_id is None:
            rows = conn.execute("SELECT id,subject,min_students,max_students,effective_from,effective_to FROM rate_rules WHERE state='confirmed' AND teacher_id IS NULL AND level=?", (level,)).fetchall()
        else:
            rows = conn.execute("SELECT id,subject,min_students,max_students,effective_from,effective_to FROM rate_rules WHERE state='confirmed' AND teacher_id=? AND level=?", (teacher_id, level)).fetchall()
        def subjects(value: str) -> set[str]:
            if value == "all":
                return {"science", "math", "physics", "chemistry", "biology", "english", "thai", "social", "other"}
            return {"chemistry", "biology"} if value == "chemistry_biology" else {value}
        for row in rows:
            if row["id"] == exclude_id:
                continue
            if not subjects(subject).intersection(subjects(row["subject"])):
                continue
            if max_students < row["min_students"] or min_students > row["max_students"]:
                continue
            if end is not None and end < row["effective_from"]:
                continue
            if row["effective_to"] is not None and row["effective_to"] < start:
                continue
            return True
        return False

    def _create_rate(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict) -> Response:
        scope = data.get("scope", "teacher")
        if scope not in {"teacher", "global"}:
            fail(400, "invalid_scope", "Choose teacher or global rate")
        teacher_id = None if scope == "global" else required_str(data, "teacher_id", 32)
        teacher = None if teacher_id is None else conn.execute("SELECT * FROM users WHERE id=? AND role='teacher'", (teacher_id,)).fetchone()
        if scope == "teacher" and not eligible_teacher(conn, teacher_id):
            fail(400, "invalid_teacher", "Select an active or pending roster teacher")
        subject = required_str(data, "subject", 40)
        level = required_str(data, "level", 10)
        if subject not in {"all", "science", "math", "physics", "chemistry", "biology", "chemistry_biology", "english", "thai", "social", "other"} or level not in {"middle", "high"} or (scope == "global" and subject != "all"):
            fail(400, "invalid_rate", "Subject or level is unsupported")
        minimum = required_int(data, "min_students", 1, 100)
        maximum = required_int(data, "max_students", minimum, 100)
        rate = required_int(data, "rate_satang_per_hour", 1, 10_000_000)
        start = required_str(data, "effective_from", 10)
        end = optional_str(data, "effective_to", 10)
        if not DATE_RE.fullmatch(start) or (end and not DATE_RE.fullmatch(end)):
            fail(400, "invalid_date", "Use YYYY-MM-DD")
        try:
            datetime.fromisoformat(start)
            if end:
                datetime.fromisoformat(end)
        except ValueError:
            fail(400, "invalid_date", "Use a real calendar date")
        if end and end < start:
            fail(400, "invalid_date", "effective_to must be on or after effective_from")
        if self._rate_overlap(conn, teacher_id, subject, level, minimum, maximum, start, end):
            fail(409, "overlapping_rate", "A confirmed rate overlaps this teacher, subject, group size and date")
        note = required_str(data, "source_note", 250)
        rate_id = uid()
        conn.execute("INSERT INTO rate_rules(id,teacher_label,teacher_id,subject,level,min_students,max_students,rate_satang_per_hour,effective_from,effective_to,state,source_note,confirmed_by,confirmed_at) VALUES(?,?,?,?,?,?,?,?,?,?,'confirmed',?,?,?)", (rate_id, teacher["name"] if teacher else "อัตรากลาง", teacher_id, subject, level, minimum, maximum, rate, start, end, note, user["id"], now()))
        self.audit(conn, user["id"], "rate_rule", rate_id, "create_confirmed", {"scope": scope, "teacher_id": teacher_id, "subject": subject, "level": level, "min_students": minimum, "max_students": maximum, "rate_satang_per_hour": rate, "effective_from": start, "effective_to": end})
        return Response(201, {"rate_id": rate_id, "state": "confirmed"})

    def _matching_rate(self, conn: sqlite3.Connection, teacher_id: str, subject: str, level: str, students: int, session_date: str) -> sqlite3.Row:
        criterion = "state='confirmed' AND level=? AND min_students<=? AND max_students>=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>=?)"
        args = (level, students, students, session_date, session_date)
        subject_match = "(subject=? OR subject='all' OR (subject='chemistry_biology' AND ? IN ('chemistry','biology')))"
        specific = conn.execute(f"SELECT * FROM rate_rules WHERE teacher_id=? AND {criterion} AND {subject_match}", (teacher_id, *args, subject, subject)).fetchall()
        rates = specific if specific else conn.execute(f"SELECT * FROM rate_rules WHERE teacher_id IS NULL AND {criterion} AND subject='all'", args).fetchall()
        if len(rates) != 1:
            fail(409, "rate_review_required", "Exactly one confirmed rate must match this teacher, course and attendance count")
        return rates[0]

    def _end_rate(self, conn: sqlite3.Connection, user: sqlite3.Row, rate_id: str, data: dict) -> Response:
        rule = conn.execute("SELECT * FROM rate_rules WHERE id=? AND state='confirmed'", (rate_id,)).fetchone()
        if not rule:
            fail(404, "rate_not_found", "Confirmed rate rule not found")
        end = required_str(data, "effective_to", 10)
        if not DATE_RE.fullmatch(end):
            fail(400, "invalid_date", "Use YYYY-MM-DD")
        try:
            datetime.fromisoformat(end)
        except ValueError:
            fail(400, "invalid_date", "Use a real calendar date")
        if end < rule["effective_from"]:
            fail(400, "invalid_date", "End date must be on or after the start date")
        used = conn.execute("SELECT s.starts_at FROM time_entries t JOIN class_sessions s ON s.id=t.session_id WHERE t.rate_rule_id=? AND t.status IN ('approved','payroll_locked','paid')", (rate_id,)).fetchall()
        if any(date_in_bangkok(r["starts_at"]) > end for r in used):
            fail(409, "rate_in_use", "This end date would exclude an already approved class")
        if self._rate_overlap(conn, rule["teacher_id"], rule["subject"], rule["level"], rule["min_students"], rule["max_students"], rule["effective_from"], end, rate_id):
            fail(409, "overlapping_rate", "This end date would overlap another confirmed rate")
        old_end = rule["effective_to"]
        conn.execute("UPDATE rate_rules SET effective_to=? WHERE id=?", (end, rate_id))
        self.audit(conn, user["id"], "rate_rule", rate_id, "end", {"old_effective_to": old_end, "effective_to": end})
        return Response(200, {"rate_id": rate_id, "effective_to": end})

    def _payroll_detail(self, conn: sqlite3.Connection, run_id: str, teacher_id: str | None) -> Response:
        run = conn.execute("SELECT p.*,u.name teacher_name FROM payroll_runs p JOIN users u ON u.id=p.teacher_id WHERE p.id=?", (run_id,)).fetchone()
        if not run or (teacher_id and run["teacher_id"] != teacher_id) or (teacher_id and run["status"] not in {"draft", "finalized", "paid"}):
            fail(404, "run_not_found", "Payroll run not found")
        lines = conn.execute("SELECT pl.time_entry_id,pl.minutes,pl.rate_satang_per_hour,pl.amount_satang,pl.pricing_type,pl.override_reason,pl.override_reference,s.starts_at,c.title course_title FROM payroll_lines pl JOIN time_entries t ON t.id=pl.time_entry_id JOIN class_sessions s ON s.id=t.session_id JOIN courses c ON c.id=s.course_id WHERE pl.run_id=? ORDER BY s.starts_at", (run_id,)).fetchall()
        adjustments = conn.execute("SELECT amount_satang,reason,created_at FROM payroll_adjustments WHERE run_id=? ORDER BY created_at", (run_id,)).fetchall()
        return Response(200, {"run": dict(run), "lines": [dict(row) for row in lines], "adjustments": [dict(row) for row in adjustments]})

    def _approve_time(self, conn: sqlite3.Connection, user: sqlite3.Row, entry_id: str) -> Response:
        row = conn.execute("SELECT t.*,s.starts_at session_starts_at,c.subject,c.level,c.id course_id FROM time_entries t JOIN class_sessions s ON s.id=t.session_id JOIN courses c ON c.id=s.course_id WHERE t.id=?", (entry_id,)).fetchone()
        if not row or row["status"] != "submitted":
            fail(404, "entry_not_pending", "Pending teaching entry not found")
        attendance = conn.execute("SELECT COUNT(*) total,COUNT(a.status) marked_count,SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END) present_count FROM session_students ss LEFT JOIN attendance a ON a.session_id=ss.session_id AND a.student_id=ss.student_id WHERE ss.session_id=?", (row["session_id"],)).fetchone()
        if attendance["marked_count"] != attendance["total"] or attendance["total"] == 0:
            fail(409, "attendance_incomplete", "Record attendance for every enrolled student before approval")
        students = attendance["present_count"] or 0
        session_date = date_in_bangkok(row["session_starts_at"])
        rule = self._matching_rate(conn, row["teacher_id"], row["subject"], row["level"], students, session_date)
        minutes = (row["actual_end"] - row["actual_start"]) // 60 - row["break_minutes"]
        rate = rule["rate_satang_per_hour"]
        amount = (minutes * rate + 30) // 60  # round half up to the nearest satang
        conn.execute("UPDATE time_entries SET status='approved',payable_minutes=?,student_count=?,rate_rule_id=?,rate_satang_per_hour=?,amount_satang=?,pricing_type='hourly_rule',reviewed_by=?,reviewed_at=? WHERE id=? AND status='submitted'", (minutes, students, rule["id"], rate, amount, user["id"], now(), entry_id))
        event_id = self.audit(conn, user["id"], "time_entry", entry_id, "approve", {"minutes": minutes, "student_count": students, "rate_rule_id": rule["id"], "amount_satang": amount})
        self.enqueue_notice(conn, event_id, "teaching_time_approved", row["teacher_id"])
        return Response(200, {"time_entry_id": entry_id, "status": "approved", "payable_minutes": minutes, "student_count": students, "rate_satang_per_hour": rate, "amount_satang": amount, "rate_rule_id": rule["id"]})

    def _approve_override(self, conn: sqlite3.Connection, user: sqlite3.Row, entry_id: str, data: dict) -> Response:
        """Approve an exceptional flat amount with a mandatory audit reason."""
        row = conn.execute("SELECT t.*,s.starts_at,c.subject,c.level FROM time_entries t JOIN class_sessions s ON s.id=t.session_id JOIN courses c ON c.id=s.course_id WHERE t.id=?", (entry_id,)).fetchone()
        if not row or row["status"] != "submitted":
            fail(404, "entry_not_pending", "Pending teaching entry not found")
        attendance = conn.execute("SELECT COUNT(*) total,COUNT(a.status) marked_count,SUM(CASE WHEN a.status='present' THEN 1 ELSE 0 END) present_count FROM session_students ss LEFT JOIN attendance a ON a.session_id=ss.session_id AND a.student_id=ss.student_id WHERE ss.session_id=?", (row["session_id"],)).fetchone()
        if attendance["total"] == 0 or attendance["marked_count"] != attendance["total"]:
            fail(409, "attendance_incomplete", "Record attendance for every enrolled student before approval")
        amount = required_int(data, "amount_satang", 1, 10_000_000)
        reason = required_str(data, "reason", 250)
        reference = required_str(data, "source_reference", 120)
        minutes = (row["actual_end"] - row["actual_start"]) // 60 - row["break_minutes"]
        students = attendance["present_count"] or 0
        conn.execute("UPDATE time_entries SET status='approved',payable_minutes=?,student_count=?,rate_rule_id=NULL,rate_satang_per_hour=NULL,amount_satang=?,pricing_type='manual_amount',override_reason=?,override_reference=?,reviewed_by=?,reviewed_at=? WHERE id=? AND status='submitted'", (minutes, students, amount, reason, reference, user["id"], now(), entry_id))
        event_id = self.audit(conn, user["id"], "time_entry", entry_id, "approve_override", {"minutes": minutes, "student_count": students, "amount_satang": amount, "reason": reason, "source_reference": reference})
        self.enqueue_notice(conn, event_id, "teaching_time_approved", row["teacher_id"])
        return Response(200, {"time_entry_id": entry_id, "status": "approved", "pricing_type": "manual_amount", "payable_minutes": minutes, "student_count": students, "amount_satang": amount, "reason": reason, "source_reference": reference})

    def _reject_time(self, conn: sqlite3.Connection, user: sqlite3.Row, entry_id: str, data: dict) -> Response:
        reason = required_str(data, "reason", 250)
        updated = conn.execute("UPDATE time_entries SET status='rejected',rejection_reason=?,reviewed_by=?,reviewed_at=? WHERE id=? AND status='submitted'", (reason, user["id"], now(), entry_id)).rowcount
        if not updated:
            fail(404, "entry_not_pending", "Pending teaching entry not found")
        event_id = self.audit(conn, user["id"], "time_entry", entry_id, "reject", {"reason": reason})
        teacher_id = conn.execute("SELECT teacher_id FROM time_entries WHERE id=?", (entry_id,)).fetchone()["teacher_id"]
        self.enqueue_notice(conn, event_id, "teaching_time_rejected", teacher_id)
        return Response(200, {"time_entry_id": entry_id, "status": "rejected"})

    def _record_payment(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict, claim_id: str | None) -> Response:
        invoice_id = required_str(data, "invoice_id", 32)
        amount = required_int(data, "amount_satang", 1, 100_000_000_00)
        method = required_str(data, "method", 30)
        reference = required_str(data, "reference", 120)
        invoice = conn.execute("SELECT * FROM invoices WHERE id=? AND status!='void'", (invoice_id,)).fetchone()
        if not invoice:
            fail(404, "invoice_not_found", "Invoice is unavailable")
        if claim_id is None and conn.execute(
            "SELECT 1 FROM payment_claims WHERE invoice_id=? AND status='pending'",
            (invoice_id,),
        ).fetchone():
            fail(409, "claim_pending", "Review the pending payment claim before recording a direct payment")
        paid = conn.execute("SELECT COALESCE(SUM(amount_satang),0) FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
        if conn.execute("SELECT 1 FROM payments WHERE reference=? COLLATE NOCASE", (reference,)).fetchone():
            fail(409, "duplicate_payment", "This payment reference was already recorded")
        if paid + amount > invoice["amount_satang"]:
            fail(409, "payment_exceeds_balance", "Payment exceeds invoice balance")
        payment_id = uid()
        paid_at = now()
        conn.execute("INSERT INTO payments(id,invoice_id,amount_satang,method,reference,claim_id,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)", (payment_id, invoice_id, amount, method, reference, claim_id, user["id"], paid_at))
        student = conn.execute("SELECT name FROM users WHERE id=?", (invoice["student_id"],)).fetchone()
        course = conn.execute(
            "SELECT c.course_code,c.title FROM enrollments e JOIN courses c ON c.id=e.course_id WHERE e.id=?",
            (invoice["enrollment_id"],),
        ).fetchone() if invoice["enrollment_id"] else None
        document_id = uid()
        document_number = receipt_number(payment_id, paid_at)
        conn.execute(
            "INSERT INTO receipts(id,payment_id,receipt_number,invoice_id,invoice_number,"
            "student_id,student_name,description,enrollment_id,course_code,course_title,"
            "amount_satang,method,reference,paid_at,issued_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (document_id, payment_id, document_number, invoice_id, invoice["invoice_number"],
             invoice["student_id"], student["name"], invoice["description"],
             invoice["enrollment_id"], course["course_code"] if course else None,
             course["title"] if course else None, amount,
             method, reference, paid_at, paid_at),
        )
        new_status = "paid" if paid + amount == invoice["amount_satang"] else "partial"
        conn.execute("UPDATE invoices SET status=? WHERE id=?", (new_status, invoice_id))
        event_id = self.audit(conn, user["id"], "payment", payment_id, "record", {"invoice_id": invoice_id, "amount_satang": amount, "status": new_status})
        if claim_id is None:
            self.enqueue_notice(conn, event_id, "payment_recorded", invoice["student_id"])
        return Response(201, {"payment_id": payment_id, "receipt_id": document_id,
                              "receipt_number": document_number, "invoice_status": new_status,
                              "remaining_satang": invoice["amount_satang"] - paid - amount})

    def _review_claim(self, conn: sqlite3.Connection, user: sqlite3.Row, claim_id: str, action: str) -> Response:
        claim = conn.execute("SELECT * FROM payment_claims WHERE id=? AND status='pending'", (claim_id,)).fetchone()
        if not claim:
            fail(404, "claim_not_pending", "Pending payment claim not found")
        if action == "reject":
            conn.execute("UPDATE payment_claims SET status='rejected',reviewed_by=?,reviewed_at=? WHERE id=?", (user["id"], now(), claim_id))
            event_id = self.audit(conn, user["id"], "payment_claim", claim_id, "reject")
            self.enqueue_notice(conn, event_id, "payment_claim_rejected", claim["student_id"])
            return Response(200, {"claim_id": claim_id, "status": "rejected"})
        payment = self._record_payment(conn, user, {"invoice_id": claim["invoice_id"], "amount_satang": claim["amount_satang"], "method": "claim_verified", "reference": claim["reference"]}, claim_id)
        conn.execute("UPDATE payment_claims SET status='approved',reviewed_by=?,reviewed_at=? WHERE id=?", (user["id"], now(), claim_id))
        event_id = self.audit(conn, user["id"], "payment_claim", claim_id, "approve", {"payment_id": payment.body["payment_id"]})
        self.enqueue_notice(conn, event_id, "payment_claim_approved", claim["student_id"])
        return Response(200, {"claim_id": claim_id, "status": "approved", **payment.body})

    def _create_payroll(self, conn: sqlite3.Connection, user: sqlite3.Row, data: dict) -> Response:
        teacher_id = required_str(data, "teacher_id", 32)
        period = required_str(data, "period", 7)
        if not MONTH_RE.fullmatch(period):
            fail(400, "invalid_period", "Use YYYY-MM")
        if not conn.execute("SELECT 1 FROM users WHERE id=? AND role='teacher'", (teacher_id,)).fetchone():
            fail(400, "invalid_teacher", "Teacher does not exist")
        candidates = conn.execute("SELECT t.*,s.starts_at session_starts_at FROM time_entries t JOIN class_sessions s ON s.id=t.session_id WHERE t.teacher_id=? AND t.status='approved' AND NOT EXISTS(SELECT 1 FROM payroll_lines pl WHERE pl.time_entry_id=t.id) ORDER BY s.starts_at", (teacher_id,)).fetchall()
        entries = [e for e in candidates if bangkok_month(e["session_starts_at"]) == period]
        if not entries:
            fail(409, "no_payable_entries", "No approved unpaid teaching entries for this teacher and period")
        run_id = uid()
        total = sum(e["amount_satang"] for e in entries)
        conn.execute("INSERT INTO payroll_runs(id,teacher_id,period,total_satang,created_by,created_at) VALUES(?,?,?,?,?,?)", (run_id, teacher_id, period, total, user["id"], now()))
        conn.executemany("INSERT INTO payroll_lines VALUES(?,?,?,?,?,?,?,?,?,?)", [(uid(), run_id, e["id"], e["rate_rule_id"], e["payable_minutes"], e["rate_satang_per_hour"], e["amount_satang"], e["pricing_type"], e["override_reason"], e["override_reference"]) for e in entries])
        self.audit(conn, user["id"], "payroll_run", run_id, "create", {"teacher_id": teacher_id, "period": period, "line_count": len(entries), "total_satang": total})
        return Response(201, {"run_id": run_id, "status": "draft", "period": period, "line_count": len(entries), "total_satang": total})

    def _payroll_action(self, conn: sqlite3.Connection, user: sqlite3.Row, run_id: str, action: str, data: dict) -> Response:
        run = conn.execute("SELECT * FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            fail(404, "run_not_found", "Payroll run not found")
        if action == "adjustments":
            if run["status"] != "draft":
                fail(409, "run_locked", "Only draft runs can be adjusted")
            amount = required_int(data, "amount_satang", -100_000_000_00, 100_000_000_00)
            if amount == 0 or run["total_satang"] + amount < 0:
                fail(400, "invalid_adjustment", "Adjustment cannot be zero or make the total negative")
            reason = required_str(data, "reason", 250)
            adjustment_id = uid()
            conn.execute("INSERT INTO payroll_adjustments VALUES(?,?,?,?,?,?)", (adjustment_id, run_id, amount, reason, user["id"], now()))
            conn.execute("UPDATE payroll_runs SET total_satang=total_satang+? WHERE id=?", (amount, run_id))
            self.audit(conn, user["id"], "payroll_adjustment", adjustment_id, "create", {"run_id": run_id, "amount_satang": amount, "reason": reason})
            return Response(201, {"adjustment_id": adjustment_id, "total_satang": run["total_satang"] + amount})
        if action == "cancel":
            if run["status"] != "draft":
                fail(409, "run_locked", "Only draft runs can be cancelled")
            conn.execute("DELETE FROM payroll_lines WHERE run_id=?", (run_id,))
            conn.execute("DELETE FROM payroll_adjustments WHERE run_id=?", (run_id,))
            conn.execute("UPDATE payroll_runs SET status='cancelled',total_satang=0 WHERE id=?", (run_id,))
            self.audit(conn, user["id"], "payroll_run", run_id, "cancel")
            return Response(200, {"run_id": run_id, "status": "cancelled"})
        if action == "finalize":
            if run["status"] != "draft":
                fail(409, "run_locked", "Only draft runs can be finalized")
            conn.execute("UPDATE payroll_runs SET status='finalized',finalized_at=? WHERE id=?", (now(), run_id))
            conn.execute("UPDATE time_entries SET status='payroll_locked' WHERE id IN (SELECT time_entry_id FROM payroll_lines WHERE run_id=?) AND status='approved'", (run_id,))
            self.audit(conn, user["id"], "payroll_run", run_id, "finalize", {"total_satang": run["total_satang"]})
            return Response(200, {"run_id": run_id, "status": "finalized", "total_satang": run["total_satang"]})
        if action == "mark-paid":
            if run["status"] != "finalized":
                fail(409, "run_not_finalized", "Finalize this run first")
            reference = required_str(data, "reference", 120)
            conn.execute("UPDATE payroll_runs SET status='paid',paid_at=?,payment_reference=? WHERE id=?", (now(), reference, run_id))
            conn.execute("UPDATE time_entries SET status='paid' WHERE id IN (SELECT time_entry_id FROM payroll_lines WHERE run_id=?) AND status='payroll_locked'", (run_id,))
            event_id = self.audit(conn, user["id"], "payroll_run", run_id, "mark_paid", {"reference": reference, "total_satang": run["total_satang"]})
            self.enqueue_notice(conn, event_id, "payroll_paid", run["teacher_id"])
            return Response(200, {"run_id": run_id, "status": "paid", "total_satang": run["total_satang"]})
        fail(404, "not_found", "Action not found")


