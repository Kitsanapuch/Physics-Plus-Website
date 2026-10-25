"""Private, retryable Turso-to-Google-Sheets mirror.

Turso remains the source of truth. Only dedicated DB_* tabs are modified; the
owner's existing payroll formulas and hand-maintained tabs are never cleared.
The application stores a dirty revision in the same transaction as each write,
then leases a bounded batch for delivery after commit.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import TYPE_CHECKING

from backend import BANGKOK, SHEET_DATASETS, bangkok_month, iso, now, uid

if TYPE_CHECKING:
    from backend import PhysicsPlusApp


REPORT_HEADERS = {
    "students": ("student_code", "name", "email", "status", "grade", "school", "guardian_name", "guardian_phone", "guardian_relationship", "created_at", "internal_id"),
    "teachers": ("id", "teacher_code", "name", "email", "status", "created_at"),
    "courses": ("course_code", "subject_code", "title", "subject", "level", "price_satang", "capacity", "active", "teacher_name", "teacher_code", "created_at", "internal_id"),
    "classes": ("id", "room_name", "starts_at", "ends_at", "status", "course_code", "subject_code", "course_title", "teacher_code", "teacher_name", "roster_count", "created_at", "cancellation_reason", "cancelled_at"),
    "class_rosters": ("class_id", "room_name", "course_code", "course_title", "student_code", "student_name", "attendance_status", "starts_at", "ends_at"),
    "enrollments": ("id", "course_code", "course_title", "student_code", "student_name", "student_email", "status", "created_at"),
    "invoices": ("id", "invoice_number", "student_name", "student_email", "description", "amount_satang", "status", "due_date", "created_at"),
    "payments": ("id", "invoice_number", "amount_satang", "method", "reference", "recorded_at"),
    "receipts": ("id", "receipt_number", "invoice_number", "student_name", "enrollment_id", "course_code", "course_title", "description", "amount_satang", "method", "reference", "paid_at", "issued_at"),
    "claims": ("id", "invoice_number", "student_name", "amount_satang", "reference", "status", "created_at", "reviewed_at"),
    "teaching": ("id", "teacher_name", "course_title", "actual_start", "actual_end", "break_minutes", "payable_minutes", "student_count", "rate_satang_per_hour", "amount_satang", "status", "submitted_at", "reviewed_at"),
    "payroll": ("id", "teacher_name", "period", "status", "total_satang", "finalized_at", "paid_at", "payment_reference", "created_at"),
    "rates": ("id", "teacher_label", "subject", "level", "min_students", "max_students", "rate_satang_per_hour", "effective_from", "effective_to", "state", "source_note"),
    "payroll_monthly": ("period", "is_current_month", "teacher_code", "teacher_name", "submitted_count", "rejected_count", "approved_count", "approved_minutes", "approved_unrun_satang", "draft_payroll_satang", "finalized_payroll_satang", "paid_satang", "total_wages_due_satang", "generated_at"),
    "payroll_summary": ("เดือน", "รายการ", "รหัสครู", "ครูผู้สอน", "รอตรวจ (คาบ)", "ปฏิเสธ (คาบ)", "อนุมัติ (คาบ)", "ชั่วโมงอนุมัติ", "ยังไม่เข้ารอบ (บาท)", "ร่างงวด (บาท)", "รอโอน (บาท)", "จ่ายแล้ว (บาท)", "ค้างจ่าย (บาท)"),
    "payment_account": ("bank_code", "bank_name", "account_name", "account_number", "updated_at"),
}
assert tuple(REPORT_HEADERS) == SHEET_DATASETS
TAB_TITLES = {dataset: "DB_" + dataset for dataset in SHEET_DATASETS}
TAB_TITLES["payroll_summary"] = "DB_สรุปค่าจ้างครู"

# Format numeric report fields for staff without changing the stored values.
# Codes, bank account numbers and Unix timestamps deliberately stay as text or
# unformatted values so that leading zeroes and identifiers remain intact.
COMMA_INTEGER_HEADERS = {
    "price_satang", "capacity", "roster_count", "amount_satang",
    "break_minutes", "payable_minutes", "student_count",
    "rate_satang_per_hour", "total_satang", "min_students", "max_students",
    "submitted_count", "rejected_count", "approved_count", "approved_minutes",
    "approved_unrun_satang", "draft_payroll_satang",
    "finalized_payroll_satang", "paid_satang", "total_wages_due_satang",
    "รอตรวจ (คาบ)", "ปฏิเสธ (คาบ)", "อนุมัติ (คาบ)",
}
COMMA_DECIMAL_HEADERS = {
    "ชั่วโมงอนุมัติ", "ยังไม่เข้ารอบ (บาท)", "ร่างงวด (บาท)",
    "รอโอน (บาท)", "จ่ายแล้ว (บาท)", "ค้างจ่าย (บาท)",
}


class SheetSyncError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _sheet_id() -> str | None:
    value = os.getenv("PH_GOOGLE_SHEET_ID", "").strip()
    return value if re.fullmatch(r"[A-Za-z0-9_-]{20,100}", value) else None


def sheet_url() -> str | None:
    identifier = _sheet_id()
    return f"https://docs.google.com/spreadsheets/d/{identifier}/edit" if identifier else None


def sheet_configured() -> bool:
    if not _sheet_id():
        return False
    try:
        info = json.loads(os.getenv("PH_GOOGLE_SERVICE_ACCOUNT_JSON", ""))
        return (isinstance(info, dict) and info.get("type") == "service_account"
                and bool(info.get("client_email")) and bool(info.get("private_key")))
    except (TypeError, ValueError):
        return False


def _monthly_rows(conn) -> list[dict]:
    """Amounts follow the application payroll model and are grouped by class month."""
    current_period = datetime.now(BANGKOK).strftime("%Y-%m")
    teachers = conn.execute(
        "SELECT id,public_code,name FROM users WHERE role='teacher' ORDER BY name,id"
    ).fetchall()
    entries = conn.execute(
        "SELECT t.teacher_id,t.status,t.payable_minutes,t.amount_satang,s.starts_at,"
        "pl.run_id FROM time_entries t JOIN class_sessions s ON s.id=t.session_id "
        "LEFT JOIN payroll_lines pl ON pl.time_entry_id=t.id"
    ).fetchall()
    runs = conn.execute(
        "SELECT teacher_id,period,status,total_satang FROM payroll_runs"
    ).fetchall()
    by_teacher: dict[str, dict[str, dict]] = {}

    def row_for(teacher_id: str, period: str) -> dict:
        return by_teacher.setdefault(teacher_id, {}).setdefault(period, {
            "period": period, "is_current_month": period == current_period,
            "submitted_count": 0, "rejected_count": 0, "approved_count": 0,
            "approved_minutes": 0, "approved_unrun_satang": 0,
            "draft_payroll_satang": 0, "finalized_payroll_satang": 0,
            "paid_satang": 0, "total_wages_due_satang": 0,
        })

    for entry in entries:
        row = row_for(entry["teacher_id"], bangkok_month(entry["starts_at"]))
        if entry["status"] == "submitted":
            row["submitted_count"] += 1
        elif entry["status"] == "rejected":
            row["rejected_count"] += 1
        elif entry["status"] in {"approved", "payroll_locked", "paid"}:
            row["approved_count"] += 1
            row["approved_minutes"] += entry["payable_minutes"] or 0
            if entry["run_id"] is None and entry["status"] == "approved":
                row["approved_unrun_satang"] += entry["amount_satang"] or 0

    for run in runs:
        row = row_for(run["teacher_id"], run["period"])
        if run["status"] == "draft":
            row["draft_payroll_satang"] += run["total_satang"]
        elif run["status"] == "finalized":
            row["finalized_payroll_satang"] += run["total_satang"]
        elif run["status"] == "paid":
            row["paid_satang"] += run["total_satang"]

    output = []
    for teacher in teachers:
        periods = by_teacher.setdefault(teacher["id"], {})
        row_for(teacher["id"], current_period)
        for period in sorted(periods, reverse=True):
            row = periods[period]
            row["teacher_code"] = teacher["public_code"]
            row["teacher_name"] = teacher["name"]
            row["total_wages_due_satang"] = (
                row["approved_unrun_satang"] + row["draft_payroll_satang"]
                + row["finalized_payroll_satang"]
            )
            row["generated_at"] = now()
            output.append(row)
    return output


def snapshot(owner: PhysicsPlusApp, conn, dataset: str) -> list[dict]:
    if dataset == "payroll_monthly":
        return _monthly_rows(conn)
    if dataset == "payroll_summary":
        monthly = _monthly_rows(conn)
        monthly.sort(key=lambda row: (
            not row["is_current_month"], -int(row["period"].replace("-", "")),
            row["teacher_name"],
        ))
        return [{
            "เดือน": row["period"],
            "รายการ": "เดือนปัจจุบัน" if row["is_current_month"] else "ย้อนหลัง",
            "รหัสครู": row["teacher_code"],
            "ครูผู้สอน": row["teacher_name"],
            "รอตรวจ (คาบ)": row["submitted_count"],
            "ปฏิเสธ (คาบ)": row["rejected_count"],
            "อนุมัติ (คาบ)": row["approved_count"],
            "ชั่วโมงอนุมัติ": round(row["approved_minutes"] / 60, 2),
            "ยังไม่เข้ารอบ (บาท)": row["approved_unrun_satang"] / 100,
            "ร่างงวด (บาท)": row["draft_payroll_satang"] / 100,
            "รอโอน (บาท)": row["finalized_payroll_satang"] / 100,
            "จ่ายแล้ว (บาท)": row["paid_satang"] / 100,
            "ค้างจ่าย (บาท)": row["total_wages_due_satang"] / 100,
        } for row in monthly]
    if dataset == "payment_account":
        row = conn.execute(
            "SELECT bank_code,bank_name,account_name,account_number,updated_at "
            "FROM payment_account WHERE id=1"
        ).fetchone()
        return [dict(row)] if row else []
    rows: list[dict] = []
    for page in range(1, 101):
        response = owner._report(conn, dataset, {"page": [str(page)]})
        rows.extend(response.body["rows"])
        if response.body["next_page"] is None:
            return rows
    raise SheetSyncError("snapshot_too_large")


def _cell(value):
    if value is None:
        return {}
    if isinstance(value, bool):
        return {"userEnteredValue": {"boolValue": value}}
    if isinstance(value, (int, float)):
        return {"userEnteredValue": {"numberValue": value}}
    return {"userEnteredValue": {"stringValue": str(value)}}


class GoogleSheetsClient:
    """Google Sheets API writer with server-only service-account credentials."""

    def __init__(self) -> None:
        identifier = _sheet_id()
        if not identifier or not sheet_configured():
            raise SheetSyncError("not_configured")
        try:
            from google.auth.transport.requests import AuthorizedSession
            from google.oauth2 import service_account

            info = json.loads(os.environ["PH_GOOGLE_SERVICE_ACCOUNT_JSON"])
            credentials = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
            self.http = AuthorizedSession(credentials, refresh_timeout=5)
        except (ImportError, KeyError, TypeError, ValueError):
            raise SheetSyncError("invalid_configuration") from None
        self.base = f"https://sheets.googleapis.com/v4/spreadsheets/{identifier}"

    def _request(self, method: str, suffix: str, body: dict | None = None) -> dict:
        try:
            response = self.http.request(method, self.base + suffix, json=body, timeout=5)
        except Exception:
            raise SheetSyncError("provider_unavailable") from None
        if not 200 <= response.status_code < 300:
            raise SheetSyncError(f"provider_http_{response.status_code}")
        try:
            return response.json()
        except (TypeError, ValueError):
            raise SheetSyncError("invalid_provider_response") from None

    def write(self, snapshots: dict[str, list[dict]]) -> None:
        metadata = self._request(
            "GET", "?fields=sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
        )
        properties = {sheet["properties"]["title"]: sheet["properties"]
                      for sheet in metadata.get("sheets", []) if "properties" in sheet}
        missing = [TAB_TITLES[dataset] for dataset in snapshots if TAB_TITLES[dataset] not in properties]
        if missing:
            try:
                self._request("POST", ":batchUpdate", {"requests": [
                    {"addSheet": {"properties": {"title": title,
                     "gridProperties": {"rowCount": 1000, "columnCount": 26,
                                        "frozenRowCount": 1}}}}
                    for title in missing
                ]})
            except SheetSyncError as exc:
                # A concurrent worker may have created the same tab. Verify by
                # re-reading metadata before treating a 400 as a real failure.
                if exc.code != "provider_http_400":
                    raise
            metadata = self._request(
                "GET", "?fields=sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))"
            )
            properties = {sheet["properties"]["title"]: sheet["properties"]
                          for sheet in metadata.get("sheets", []) if "properties" in sheet}
            if any(title not in properties for title in missing):
                raise SheetSyncError("tab_creation_failed")

        requests = []
        for dataset, rows in snapshots.items():
            headers = REPORT_HEADERS[dataset]
            # Show identifiers staff can recognize first. The UUID remains
            # available for reconciliation, clearly labeled at the far right.
            values = [list(headers)] + [
                [row.get("id" if key == "internal_id" else key) for key in headers]
                for row in rows
            ]
            sheet = properties[TAB_TITLES[dataset]]
            sheet_id = sheet["sheetId"]
            grid = sheet.get("gridProperties", {})
            row_count = int(grid.get("rowCount", 1000))
            column_count = int(grid.get("columnCount", 26))
            needed_rows = max(2, len(values))
            needed_columns = len(headers)
            if needed_rows > row_count or needed_columns > column_count:
                requests.append({"updateSheetProperties": {
                    "properties": {"sheetId": sheet_id, "gridProperties": {
                        "rowCount": max(row_count, needed_rows),
                        "columnCount": max(column_count, needed_columns),
                        "frozenRowCount": 1,
                    }},
                    "fields": "gridProperties.rowCount,gridProperties.columnCount,gridProperties.frozenRowCount",
                }})
                row_count = max(row_count, needed_rows)
            # Clearing and replacing a dedicated tab in one batchUpdate keeps
            # retries idempotent and never touches hand-maintained formula tabs.
            requests.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0,
                          "endRowIndex": row_count, "startColumnIndex": 0,
                          "endColumnIndex": needed_columns},
                "cell": {}, "fields": "userEnteredValue",
            }})
            requests.append({"updateCells": {
                "start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
                "rows": [{"values": [_cell(value) for value in row]} for row in values],
                "fields": "userEnteredValue",
            }})
            requests.append({"repeatCell": {
                "range": {"sheetId": sheet_id, "startRowIndex": 0,
                          "endRowIndex": 1, "startColumnIndex": 0,
                          "endColumnIndex": needed_columns},
                "cell": {"userEnteredFormat": {"backgroundColor": {
                    "red": 0.99, "green": 0.80, "blue": 0.14},
                    "textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.backgroundColor,userEnteredFormat.textFormat.bold",
            }})
            for column_index, header in enumerate(headers):
                if header not in COMMA_INTEGER_HEADERS and header not in COMMA_DECIMAL_HEADERS:
                    continue
                pattern = "#,##0.00" if header in COMMA_DECIMAL_HEADERS else "#,##0"
                requests.append({"repeatCell": {
                    "range": {"sheetId": sheet_id, "startRowIndex": 1,
                              "endRowIndex": row_count,
                              "startColumnIndex": column_index,
                              "endColumnIndex": column_index + 1},
                    "cell": {"userEnteredFormat": {"numberFormat": {
                        "type": "NUMBER", "pattern": pattern}}},
                    "fields": "userEnteredFormat.numberFormat",
                }})
        encoded = json.dumps({"requests": requests}, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 6_000_000:
            raise SheetSyncError("snapshot_too_large")
        self._request("POST", ":batchUpdate", json.loads(encoded))


def dispatch(owner: PhysicsPlusApp, *, limit: int = 20, client=None) -> dict[str, int]:
    """Lease dirty datasets, mirror snapshots, then acknowledge exact revisions."""
    result = {"processed": 0, "synced": 0, "retrying": 0}
    if client is None and not sheet_configured():
        return result
    limit = max(0, min(int(limit), len(SHEET_DATASETS)))
    if limit == 0:
        return result
    current = now()
    lease_token = uid()
    with owner.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        dirty = conn.execute(
            "SELECT dataset,revision,attempts FROM sheet_sync_state "
            "WHERE revision>synced_revision AND next_attempt_at<=? "
            "AND (lease_until IS NULL OR lease_until<=?) "
            "ORDER BY updated_at,dataset LIMIT ?",
            (current, current, limit),
        ).fetchall()
        for row in dirty:
            conn.execute(
                "UPDATE sheet_sync_state SET lease_token=?,lease_until=?,attempts=attempts+1 "
                "WHERE dataset=?",
                (lease_token, current + 120, row["dataset"]),
            )
        conn.commit()
    if not dirty:
        return result
    result["processed"] = len(dirty)
    try:
        with owner.connect() as conn:
            snapshots = {row["dataset"]: snapshot(owner, conn, row["dataset"])
                         for row in dirty}
        writer = client or GoogleSheetsClient()
        writer.write(snapshots)
    except Exception as exc:
        code = exc.code if isinstance(exc, SheetSyncError) else "sync_exception"
        with owner.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in dirty:
                delay = min(3600, 30 * (2 ** min(row["attempts"], 7)))
                conn.execute(
                    "UPDATE sheet_sync_state SET lease_token=NULL,lease_until=NULL,"
                    "next_attempt_at=?,last_error_code=? "
                    "WHERE dataset=? AND lease_token=?",
                    (now() + delay, code, row["dataset"], lease_token),
                )
            conn.commit()
        result["retrying"] = len(dirty)
        return result

    with owner.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for row in dirty:
            updated = conn.execute(
                "UPDATE sheet_sync_state SET synced_revision=MAX(synced_revision,?),"
                "synced_at=?,lease_token=NULL,lease_until=NULL,"
                "next_attempt_at=0,last_error_code=NULL "
                "WHERE dataset=? AND lease_token=?",
                (row["revision"], now(), row["dataset"], lease_token),
            ).rowcount
            if updated:
                result["synced"] += 1
            else:
                # A stalled worker may have lost its lease while Google was
                # processing the write. Its older snapshot may have landed
                # after a newer worker's update, so force another refresh.
                conn.execute(
                    "UPDATE sheet_sync_state SET revision=revision+1,"
                    "next_attempt_at=0 WHERE dataset=?",
                    (row["dataset"],),
                )
        conn.commit()
    return result
