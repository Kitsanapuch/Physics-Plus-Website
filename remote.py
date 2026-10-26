"""Remote, transactional Turso storage for the Physics Plus API.

Both remote drivers keep one connection open for each API request, preserving
the explicit BEGIN IMMEDIATE / COMMIT boundaries in backend.PhysicsPlusApp.
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import threading
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import libsql
import turso_serverless

from backend import (
    COURSE_CATALOG,
    COURSE_SEED_MIGRATION,
    BRAND_CODES_MIGRATION,
    STUDENT_CODE_MIGRATION,
    TEACHER_ROSTER_MIGRATION,
    PhysicsPlusApp,
    PORTAL_FEATURE_SCHEMA,
    RATE_GRID,
    RATE_GRID_EFFECTIVE_FROM,
    RATE_GRID_MIGRATION,
    Response,
    SCHEMA,
    SLIP_VERIFICATION_SCHEMA,
    SHEET_SYNC_SCHEMA,
    SHEET_DATASETS,
    fail,
    migrate_outbox_schema,
    migrate_academic_schema,
    migrate_student_codes,
    migrate_nullable_course_teachers,
    migrate_brand_codes,
    migrate_receipts,
    seed_initial_courses,
    seed_individual_courses,
    seed_initial_teachers,
    seed_payment_account_and_sheet_queue,
    now,
    uid,
)


THROTTLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS login_throttle (
  key_hash TEXT PRIMARY KEY,
  window_started INTEGER NOT NULL,
  attempts INTEGER NOT NULL CHECK(attempts >= 0)
);
"""

SHEET_INIT_REFRESH_MIGRATION = "sheet_sync_post_migration_refresh_2026_10_23"


class LibsqlRow(tuple):
    """Give libsql's tuple results the index and column-name API of sqlite3.Row."""

    def __new__(cls, values: tuple, columns: tuple[str, ...]) -> LibsqlRow:
        row = super().__new__(cls, values)
        row._columns = columns
        return row

    def __getitem__(self, key: int | slice | str):
        if isinstance(key, str):
            for index, name in enumerate(self._columns):
                if name.casefold() == key.casefold():
                    return super().__getitem__(index)
            raise IndexError(f"No item with that key: {key}")
        return super().__getitem__(key)

    def keys(self) -> list[str]:
        return list(self._columns)


class LibsqlCursor:
    def __init__(self, cursor: libsql.Cursor) -> None:
        self._cursor = cursor
        self._columns = tuple(column[0] for column in cursor.description or ())

    def __getattr__(self, name: str):
        return getattr(self._cursor, name)

    def _row(self, values: tuple | None) -> LibsqlRow | None:
        return None if values is None else LibsqlRow(values, self._columns)

    def fetchone(self) -> LibsqlRow | None:
        return self._row(self._cursor.fetchone())

    def fetchall(self) -> list[LibsqlRow]:
        return [LibsqlRow(values, self._columns) for values in self._cursor.fetchall() or ()]

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row


class LibsqlConnection:
    """Adapt the native libsql driver's missing sqlite3 conveniences."""

    def __init__(self, connection: libsql.Connection) -> None:
        self._connection = connection

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    @staticmethod
    def _call(operation, *args):
        try:
            return operation(*args)
        except ValueError as exc:
            # The native libsql driver reports SQLite constraint violations as
            # ValueError, while the domain API uses sqlite3.IntegrityError.
            if str(exc).lower().startswith((
                "unique constraint failed", "foreign key constraint failed",
                "check constraint failed", "not null constraint failed",
            )):
                raise sqlite3.IntegrityError("Remote database constraint failed") from exc
            raise

    def execute(self, sql: str, parameters: tuple | list = ()) -> LibsqlCursor:
        return LibsqlCursor(self._call(self._connection.execute, sql, parameters))

    def executemany(self, sql: str, parameters: list[tuple]) -> LibsqlCursor:
        return LibsqlCursor(self._call(self._connection.executemany, sql, parameters))

    def executescript(self, script: str) -> None:
        # libsql 0.1.11's executescript silently discards execute_batch errors.
        # Run complete statements individually so schema setup fails closed.
        start = 0
        for index, character in enumerate(script):
            if character == ";" and sqlite3.complete_statement(script[start:index + 1]):
                self.execute(script[start:index + 1])
                start = index + 1
        if script[start:].strip():
            self.execute(script[start:])


class ClosingRemoteConnection:
    """Context manager with sqlite3-style commit/rollback and actual close."""

    def __init__(self, connection: turso_serverless.Connection | LibsqlConnection) -> None:
        self._connection = connection

    def __getattr__(self, name: str):
        return getattr(self._connection, name)

    def __enter__(self) -> ClosingRemoteConnection:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._connection.close()
        return False


class PersistentRateLimiter:
    """Share authentication attempt limits across Vercel function instances."""

    def __init__(self, owner: RemotePhysicsPlusApp) -> None:
        self.owner = owner

    def check(self, key: str, limit: int, period: int, *, conn=None) -> None:
        if limit < 1 or period < 1:
            raise ValueError("invalid rate limit")
        digest = hmac.new(
            self.owner.auth_token.encode("utf-8"), key.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        current = now()
        if conn is None:
            with self.owner.connect() as own_conn:
                self._check(own_conn, digest, current, limit, period)
        else:
            self._check(conn, digest, current, limit, period)

    def check_login_attempt(self, client_ip: str, identifier: str, *, conn=None) -> None:
        """Atomically count both login limits with one remote write/commit.

        If the IP is blocked, the identifier is untouched. If only the
        identifier is blocked, the IP attempt remains counted. Materializing
        the IP decision before either upsert preserves that order even when
        the accepted IP attempt reaches its limit in this statement.
        """
        ip_digest = hmac.new(
            self.owner.auth_token.encode("utf-8"),
            f"ip:{client_ip}".encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        login_digest = hmac.new(
            self.owner.auth_token.encode("utf-8"),
            f"login:{identifier}".encode("utf-8"), hashlib.sha256,
        ).hexdigest()
        current = now()
        if conn is None:
            with self.owner.connect() as own_conn:
                self._check_login_attempt(own_conn, ip_digest, login_digest, current)
        else:
            self._check_login_attempt(conn, ip_digest, login_digest, current)

    @staticmethod
    def _check_login_attempt(conn, ip_digest: str, login_digest: str, current: int) -> None:
        if current % 97 == 0:
            conn.execute("DELETE FROM login_throttle WHERE window_started < ?", (current - 24 * 3600,))
        cutoff = current - 15 * 60
        accepted = conn.execute(
            "WITH limits(key_hash,cap,cutoff) AS MATERIALIZED "
            "(VALUES (?,?,?),(?,?,?)),"
            "ip_permitted(allowed) AS MATERIALIZED ("
            "SELECT COALESCE((SELECT window_started<=? OR attempts<? "
            "FROM login_throttle WHERE key_hash=?),1)) "
            "INSERT INTO login_throttle(key_hash,window_started,attempts) "
            "SELECT limits.key_hash,?,1 FROM limits,ip_permitted "
            "WHERE ip_permitted.allowed=1 AND true "
            "ON CONFLICT(key_hash) DO UPDATE SET "
            "window_started=CASE WHEN login_throttle.window_started<="
            "(SELECT cutoff FROM limits WHERE limits.key_hash=excluded.key_hash) "
            "THEN excluded.window_started ELSE login_throttle.window_started END,"
            "attempts=CASE WHEN login_throttle.window_started<="
            "(SELECT cutoff FROM limits WHERE limits.key_hash=excluded.key_hash) "
            "THEN 1 ELSE login_throttle.attempts+1 END "
            "WHERE login_throttle.window_started<="
            "(SELECT cutoff FROM limits WHERE limits.key_hash=excluded.key_hash) "
            "OR login_throttle.attempts<"
            "(SELECT cap FROM limits WHERE limits.key_hash=excluded.key_hash) "
            "RETURNING key_hash",
            (ip_digest, 20, cutoff, login_digest, 10, cutoff,
             cutoff, 20, ip_digest, current),
        ).fetchall()
        # Count the accepted IP even when the identifier is blocked. This
        # commit must not be rolled back by a later login/authentication error.
        conn.commit()
        if {row[0] for row in accepted} != {ip_digest, login_digest}:
            fail(429, "rate_limited", "Too many attempts; try again later")

    @staticmethod
    def _check(conn, digest: str, current: int, limit: int, period: int) -> None:
        # Each key is one atomic write and commits independently. An identifier
        # rejection must not undo the IP attempt that was counted first.
        # Periodic bounded cleanup keeps this table from growing forever.
        if current % 97 == 0:
            conn.execute("DELETE FROM login_throttle WHERE window_started < ?", (current - 24 * 3600,))
        cutoff = current - period
        accepted = conn.execute(
            "INSERT INTO login_throttle(key_hash,window_started,attempts) VALUES(?,?,1) "
            "ON CONFLICT(key_hash) DO UPDATE SET "
            "window_started=CASE WHEN login_throttle.window_started<=? "
            "THEN excluded.window_started ELSE login_throttle.window_started END, "
            "attempts=CASE WHEN login_throttle.window_started<=? "
            "THEN 1 ELSE login_throttle.attempts+1 END "
            "WHERE login_throttle.window_started<=? OR login_throttle.attempts<? "
            "RETURNING attempts",
            (digest, current, cutoff, cutoff, cutoff, limit),
        ).fetchone()
        if accepted is None:
            fail(429, "rate_limited", "Too many attempts; try again later")
        conn.commit()


class RemotePhysicsPlusApp(PhysicsPlusApp):
    def __init__(
        self,
        database_url: str,
        auth_token: str,
        origin: str,
        extra_origins: set[str] | None = None,
    ) -> None:
        parsed = urlsplit(database_url)
        if parsed.scheme not in {"turso", "libsql"} or not parsed.hostname:
            raise ValueError("TURSO_DATABASE_URL must be a turso:// or libsql:// database URL")
        if parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("TURSO_DATABASE_URL must not contain credentials or query text")
        if not auth_token.strip():
            raise ValueError("TURSO_AUTH_TOKEN is required")
        self.database_url = database_url.rstrip("/")
        self.database_scheme = parsed.scheme
        self.auth_token = auth_token
        self._sheet_schema_ready = False
        self._sheet_schema_lock = threading.Lock()
        self._student_code_ready = False
        self._student_code_lock = threading.Lock()
        self._payment_slips_ready = False
        self._payment_slips_lock = threading.Lock()
        self._slip_verification_ready = False
        self._slip_verification_lock = threading.Lock()
        self._portal_features_ready = False
        self._portal_features_lock = threading.Lock()
        self._foreign_keys_verified = False
        self._foreign_keys_lock = threading.Lock()
        self._session_cleanup_trigger_ready = False
        self._session_cleanup_lock = threading.Lock()
        # Broad historical migrations remain an explicit management step.
        # Additive sync/settings tables and the narrow student-code migration
        # are safe to apply once on the first connection of each warm worker.
        super().__init__(Path("/__remote_turso__"), origin, None)
        for extra in extra_origins or set():
            parsed_origin = urlsplit(extra)
            if parsed_origin.scheme != "https" or not parsed_origin.netloc or parsed_origin.path or parsed_origin.query or parsed_origin.fragment:
                raise ValueError("Additional public origins must be exact HTTPS origins")
            self.allowed_origins.add(extra.rstrip("/"))
        self.limiter = PersistentRateLimiter(self)

    def _init_db(self) -> None:
        pass

    def connect(self) -> ClosingRemoteConnection:
        if self.database_scheme == "libsql":
            connection = LibsqlConnection(libsql.connect(
                database=self.database_url,
                auth_token=self.auth_token,
                isolation_level=None,
                timeout=30.0,
            ))
        else:
            connection = turso_serverless.connect(
                self.database_url,
                auth_token=self.auth_token,
                isolation_level=None,
            )
        try:
            if self.database_scheme == "turso":
                connection.row_factory = turso_serverless.Row
            connection.execute("PRAGMA foreign_keys=ON")
            # Turso's remote libSQL protocol rejects PRAGMA busy_timeout;
            # libsql.connect(timeout=30.0) configures the client instead.
            if self.database_scheme == "turso":
                connection.execute("PRAGMA busy_timeout=30000")
            # Every fresh connection enables FK checks before any transaction.
            # Verify the driver's behavior once per warm worker, rather than
            # paying for another remote read on every API request.
            if not self._foreign_keys_verified:
                with self._foreign_keys_lock:
                    if not self._foreign_keys_verified:
                        enabled = connection.execute("PRAGMA foreign_keys").fetchone()
                        if enabled is None or enabled[0] != 1:
                            raise RuntimeError("Remote database must enforce foreign keys")
                        self._foreign_keys_verified = True
            # A deployed database already has these tables and migration
            # marker. Inspect it once per warm worker instead of issuing DDL,
            # 16 seed writes and a migration transaction on every cold start.
            if (not self._sheet_schema_ready or not self._student_code_ready
                    or not self._payment_slips_ready or not self._slip_verification_ready
                    or not self._portal_features_ready
                    or not self._session_cleanup_trigger_ready):
                present = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table','trigger') AND name IN "
                    "('users','sessions','sessions_cleanup_expired','app_migrations','code_counters','audit_events',"
                    "'payment_claims',"
                    "'sheet_sync_state','payment_account','payment_slips','payment_claim_verifications',"
                    "'course_formats','course_progress_targets','teacher_availability',"
                    "'inbox_notifications','assignments','individual_monthly_invoices',"
                    "'individual_billing_lines')"
                ).fetchall()}
                if not self._session_cleanup_trigger_ready and "sessions" in present:
                    with self._session_cleanup_lock:
                        if not self._session_cleanup_trigger_ready:
                            if "sessions_cleanup_expired" not in present:
                                connection.execute(
                                    "CREATE TRIGGER IF NOT EXISTS sessions_cleanup_expired "
                                    "AFTER INSERT ON sessions BEGIN "
                                    "DELETE FROM sessions WHERE expires_at<=NEW.created_at; END"
                                )
                            self._session_cleanup_trigger_ready = True
                if not self._portal_features_ready and "users" in present:
                    with self._portal_features_lock:
                        if not self._portal_features_ready:
                            if not {"course_formats", "course_progress_targets", "teacher_availability", "inbox_notifications", "assignments", "individual_monthly_invoices", "individual_billing_lines"}.issubset(present):
                                connection.executescript(PORTAL_FEATURE_SCHEMA)
                            self._portal_features_ready = True
                if not self._payment_slips_ready and "users" in present:
                    with self._payment_slips_lock:
                        if not self._payment_slips_ready:
                            if "payment_slips" not in present:
                                connection.execute(
                                    "CREATE TABLE IF NOT EXISTS payment_slips ("
                                    "claim_id TEXT PRIMARY KEY REFERENCES payment_claims(id),"
                                    "mime_type TEXT NOT NULL CHECK(mime_type IN ('image/jpeg','image/png','image/webp')) ,"
                                    "content BLOB NOT NULL,sha256 TEXT NOT NULL UNIQUE,"
                                    "uploaded_at INTEGER NOT NULL,CHECK(length(content) BETWEEN 1 AND 870400))"
                                )
                            self._payment_slips_ready = True
                if not self._slip_verification_ready and "payment_claims" in present:
                    with self._slip_verification_lock:
                        if not self._slip_verification_ready:
                            if "payment_claim_verifications" not in present:
                                connection.executescript(SLIP_VERIFICATION_SCHEMA)
                            self._slip_verification_ready = True
                if not self._sheet_schema_ready:
                    with self._sheet_schema_lock:
                        if not self._sheet_schema_ready:
                            if {"sheet_sync_state", "payment_account"}.issubset(present):
                                seed_needed = connection.execute(
                                    "SELECT (SELECT COUNT(*) FROM sheet_sync_state),"
                                    "(SELECT COUNT(*) FROM payment_account WHERE id=1)"
                                ).fetchone()
                            else:
                                connection.executescript(SHEET_SYNC_SCHEMA)
                                seed_needed = (0, 0)
                                present.update({"sheet_sync_state", "payment_account"})
                            if "users" in present and (seed_needed[0] < len(SHEET_DATASETS) or seed_needed[1] != 1):
                                seed_payment_account_and_sheet_queue(connection)
                            self._sheet_schema_ready = True
                # This narrow migration changes only visible student codes.
                # The marker check is one read on current deployments; older
                # schemas still receive the full migration before serving.
                if not self._student_code_ready:
                    with self._student_code_lock:
                        if not self._student_code_ready:
                            required = {"users", "app_migrations", "code_counters",
                                        "audit_events", "sheet_sync_state"}
                            if required.issubset(present):
                                applied = connection.execute(
                                    "SELECT 1 FROM app_migrations WHERE key=?",
                                    (STUDENT_CODE_MIGRATION,),
                                ).fetchone()
                                if applied:
                                    self._student_code_ready = True
                                elif any(row[1] == "public_code" for row in connection.execute(
                                    "PRAGMA table_info(users)"
                                ).fetchall()):
                                    migrate_student_codes(connection)
                                    self._student_code_ready = True
            return ClosingRemoteConnection(connection)
        except BaseException:
            connection.close()
            raise

    def initialize_remote(self) -> None:
        """Run idempotent schema/rate initialization from a trusted CLI."""
        with self.connect() as conn:
            conn.executescript(SCHEMA + THROTTLE_SCHEMA)
            self._payment_slips_ready = True
            self._slip_verification_ready = True
            self._portal_features_ready = True
            self._session_cleanup_trigger_ready = True
            migrate_outbox_schema(conn)
            migrate_academic_schema(conn)
            migrate_student_codes(conn)
            migrate_nullable_course_teachers(conn)
            migrate_brand_codes(conn)
            seed_initial_teachers(conn)
            seed_initial_courses(conn)
            seed_individual_courses(conn)
            migrate_receipts(conn)
            seed_payment_account_and_sheet_queue(conn)
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute(
                "SELECT 1 FROM app_migrations WHERE key=?", (RATE_GRID_MIGRATION,)
            ).fetchone():
                conn.execute(
                    "DELETE FROM rate_rules WHERE state='pending_confirmation' "
                    "AND teacher_id IS NULL AND source_note LIKE 'Inferred hourly rate%'"
                )
                conn.executemany(
                    "INSERT INTO rate_rules(id,teacher_label,subject,level,min_students,max_students,"
                    "rate_satang_per_hour,effective_from,state,source_note,confirmed_at) "
                    "VALUES(?,'อัตรากลาง','all',?,?,?,?,?,'confirmed',?,?)",
                    [
                        (uid(), level, low, high, amount, RATE_GRID_EFFECTIVE_FROM, note, now())
                        for level, low, high, amount, note in RATE_GRID
                    ],
                )
                conn.execute(
                    "INSERT INTO app_migrations(key,applied_at) VALUES(?,?)",
                    (RATE_GRID_MIGRATION, now()),
                )
                self.audit(
                    conn,
                    None,
                    "rate_schedule",
                    RATE_GRID_MIGRATION,
                    "activate_owner_grid",
                    {"effective_from": RATE_GRID_EFFECTIVE_FROM, "tiers": len(RATE_GRID)},
                )
            # Earlier migrations can change exported rows without going through
            # audit(). Refresh a previously clean mirror once after those
            # migrations, while leaving an unsynced initial backfill alone.
            if not conn.execute(
                "SELECT 1 FROM app_migrations WHERE key=?", (SHEET_INIT_REFRESH_MIGRATION,)
            ).fetchone():
                if conn.execute(
                    "SELECT 1 FROM sheet_sync_state WHERE synced_revision>0 LIMIT 1"
                ).fetchone():
                    self._queue_all_sheet_datasets(conn)
                conn.execute(
                    "INSERT INTO app_migrations(key,applied_at) VALUES(?,?)",
                    (SHEET_INIT_REFRESH_MIGRATION, now()),
                )
            conn.commit()

    def check_remote(self) -> dict:
        """Read-only deployment readiness check for the management CLI."""
        with self.connect() as conn:
            marker = conn.execute(
                "SELECT 1 FROM app_migrations WHERE key=?", (RATE_GRID_MIGRATION,)
            ).fetchone()
            student_code_marker = conn.execute(
                "SELECT 1 FROM app_migrations WHERE key=?", (STUDENT_CODE_MIGRATION,)
            ).fetchone()
            invalid_student_codes = conn.execute(
                "SELECT COUNT(*) FROM users WHERE role='student' AND "
                "(public_code IS NULL OR length(public_code)!=5 OR "
                "public_code NOT GLOB '[0-9][0-9][0-9][0-9][0-9]' OR "
                "substr(public_code,3,3)='000')"
            ).fetchone()[0]
            admin_count, active_admin_count = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(status='active'),0) FROM users WHERE role='admin'"
            ).fetchone()
            rate_rows = conn.execute(
                "SELECT level,min_students,max_students,rate_satang_per_hour,subject,effective_to "
                "FROM rate_rules WHERE teacher_id IS NULL AND state='confirmed' AND effective_from=?",
                (RATE_GRID_EFFECTIVE_FROM,),
            ).fetchall()
            expected_rates = Counter((level, low, high, amount, "all", None)
                                     for level, low, high, amount, _ in RATE_GRID)
            actual_rates = Counter(tuple(row) for row in rate_rows)
            throttle_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='login_throttle'"
            ).fetchone()
            receipts_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='receipts'"
            ).fetchone()
            verification_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='payment_claim_verifications'"
            ).fetchone()
            payments_missing_receipts = conn.execute(
                "SELECT COUNT(*) FROM payments p LEFT JOIN receipts r ON r.payment_id=p.id WHERE r.id IS NULL"
            ).fetchone()[0] if receipts_table else -1
            outbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(notification_outbox)").fetchall()}
            sheet_columns = {row[1] for row in conn.execute("PRAGMA table_info(sheet_sync_state)").fetchall()}
            sheet_dataset_count = conn.execute("SELECT COUNT(*) FROM sheet_sync_state").fetchone()[0]
            payment_account_count = conn.execute("SELECT COUNT(*) FROM payment_account WHERE id=1").fetchone()[0]
            user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            course_columns = {row[1] for row in conn.execute("PRAGMA table_info(courses)").fetchall()}
            teacher_nullable = next((row[3] == 0 for row in conn.execute("PRAGMA table_info(courses)").fetchall()
                                     if row[1] == "teacher_id"), False)
            class_columns = {row[1] for row in conn.execute("PRAGMA table_info(class_sessions)").fetchall()}
            clauses = " OR ".join("(level=? AND subject=?)" for _ in COURSE_CATALOG)
            catalog_args = tuple(value for level, subject in COURSE_CATALOG for value in (level, subject))
            invalid_active = conn.execute(
                "SELECT COUNT(*) FROM courses WHERE active=1 AND NOT (" + clauses + ")", catalog_args
            ).fetchone()[0]
            missing_codes = conn.execute(
                "SELECT (SELECT COUNT(*) FROM users WHERE role IN ('student','teacher') AND public_code IS NULL)"
                "+(SELECT COUNT(*) FROM courses WHERE course_code IS NULL OR subject_code IS NULL)"
                "+(SELECT COUNT(*) FROM class_sessions WHERE teacher_id IS NULL OR room_name IS NULL)"
            ).fetchone()[0]
            new_migrations = conn.execute(
                "SELECT COUNT(*) FROM app_migrations WHERE key IN (?,?,?)",
                (BRAND_CODES_MIGRATION, TEACHER_ROSTER_MIGRATION, COURSE_SEED_MIGRATION),
            ).fetchone()[0]
            return {
                "schema_initialized": bool(marker) and bool(student_code_marker) and
                                      invalid_student_codes == 0 and bool(throttle_table) and
                                      {"id", "first_attempt_at", "lease_token"}.issubset(outbox_columns) and
                                      {"dataset", "revision", "synced_revision", "lease_token"}.issubset(sheet_columns) and
                                      sheet_dataset_count >= len(SHEET_DATASETS) and payment_account_count == 1 and
                                      {"public_code"}.issubset(user_columns) and
                                      {"course_code", "subject_code"}.issubset(course_columns) and
                                      {"room_name", "teacher_id"}.issubset(class_columns) and
                                      teacher_nullable and bool(receipts_table) and bool(verification_table) and
                                      payments_missing_receipts == 0 and
                                      len(COURSE_CATALOG) == 8 and invalid_active == 0 and missing_codes == 0 and new_migrations == 3,
                "admin_count": admin_count,
                "active_admin_count": active_admin_count,
                "confirmed_default_rate_count": len(rate_rows),
                "default_rate_grid_matches": actual_rates == expected_rates,
                "catalog_offering_count": len(COURSE_CATALOG),
                "invalid_active_course_count": invalid_active,
                "missing_public_code_count": missing_codes,
                "invalid_student_code_count": invalid_student_codes,
                "payments_missing_receipts": payments_missing_receipts,
                "sheet_dataset_count": sheet_dataset_count,
                "payment_account_count": payment_account_count,
            }

    def handle(self, method: str, target: str, data: dict | None, headers: dict[str, str], client_ip: str) -> Response:
        try:
            result = super().handle(method, target, data, headers, client_ip)
            if method == "GET" and target.split("?", 1)[0] == "/api/health" and result.status == 200:
                with self.connect() as conn:
                    conn.execute("SELECT 1").fetchone()
                return Response(200, {"ok": True, "database": "turso"})
            return result
        except turso_serverless.IntegrityError:
            return Response(409, {"error": {"code": "conflict", "message": "The requested change conflicts with existing data"}})

