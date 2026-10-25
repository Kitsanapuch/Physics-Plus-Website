"""Physics Plus Flask entrypoint, auto-detected by Vercel's Python runtime."""

from __future__ import annotations

import ipaddress
import json
import os
import hmac
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, make_response, redirect, request, send_from_directory
from turso_serverless import DatabaseError
from turso_serverless.protocol import ProtocolError

from backend import MAX_BODY, Response, SHEET_DATASETS
from mailer import delivery_configured, send_update_email
from remote import RemotePhysicsPlusApp
from sheets_sync import dispatch as dispatch_sheet_sync, sheet_configured


PUBLIC_DIR = Path(__file__).resolve().parent / "public"
PRIVATE_DIR = Path(__file__).resolve().parent / "private"
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY


def _exact_origin(value: str, *, allow_local_http: bool = False) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (allow_local_http and local and parsed.scheme == "http"):
        raise ValueError("PH_PUBLIC_ORIGIN must be an exact HTTPS origin")
    if not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("PH_PUBLIC_ORIGIN must contain only scheme and host")
    return value


def _public_origins() -> tuple[str, set[str]]:
    origins: list[str] = []
    configured = os.getenv("PH_PUBLIC_ORIGIN", "").strip()
    if configured:
        origins.append(_exact_origin(configured, allow_local_http=not bool(os.getenv("VERCEL"))))
    if os.getenv("VERCEL"):
        for key in ("VERCEL_URL", "VERCEL_BRANCH_URL"):
            host = os.getenv(key, "").strip()
            if host:
                if "/" in host or "@" in host or "?" in host or "#" in host:
                    raise ValueError(f"{key} must be a hostname")
                origins.append(_exact_origin("https://" + host))
    if not origins:
        raise ValueError("Set PH_PUBLIC_ORIGIN or deploy with VERCEL_URL")
    return origins[0], set(origins[1:])


@lru_cache(maxsize=1)
def _backend() -> RemotePhysicsPlusApp:
    database_url = os.getenv("TURSO_DATABASE_URL", "").strip()
    token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    origin, extra = _public_origins()
    return RemotePhysicsPlusApp(database_url, token, origin, extra)


def _client_ip() -> str:
    # Vercel overwrites these forwarding headers before the request reaches
    # the function. Ignore them when running outside Vercel.
    value = ""
    if os.getenv("VERCEL"):
        value = request.headers.get("x-vercel-forwarded-for", "") or request.headers.get("x-forwarded-for", "")
    value = value.split(",", 1)[0].strip() or (request.remote_addr or "")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return "unknown"


def _json_response(result: Response):
    response = make_response(json.dumps(result.body, ensure_ascii=False, separators=(",", ":")), result.status)
    response.headers["Content-Type"] = "application/json; charset=utf-8"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Vary"] = "Cookie, Origin"
    for key, value in result.headers.items():
        response.headers[key] = value
    return response


def _error(status: int, code: str, message: str):
    return _json_response(Response(status, {"error": {"code": code, "message": message}}))


def _dispatch_notifications(*, limit: int) -> dict[str, int] | None:
    """Deliver a bounded batch only after the business transaction committed."""
    if not delivery_configured():
        return None
    return _backend().dispatch_outbox(
        send_update_email,
        limit=limit,
        admin_email=os.getenv("PH_ADMIN_NOTIFICATION_EMAIL"),
    )


def _dispatch_sheets(*, limit: int) -> dict[str, int] | None:
    """Mirror committed changes; a failed delivery remains queued in Turso."""
    if not sheet_configured():
        return None
    return dispatch_sheet_sync(_backend(), limit=limit)


@app.errorhandler(413)
def body_too_large(_error_info):
    return _error(413, "body_too_large", "Request body exceeds the size limit")


@app.get("/api/cron/notifications")
def retry_notifications():
    secret = os.getenv("CRON_SECRET", "")
    supplied = request.headers.get("Authorization", "")
    if len(secret) < 32 or not hmac.compare_digest(supplied, "Bearer " + secret):
        return _error(401, "unauthorized", "Unauthorized")
    try:
        result = _dispatch_notifications(limit=25)
    except (DatabaseError, ProtocolError, RuntimeError, ValueError):
        return _error(503, "notification_retry_unavailable", "Retry is temporarily unavailable")
    return _json_response(Response(200, {"ok": True, "delivery": "configured" if result is not None else "disabled", "batch": result}))


@app.get("/api/cron/sheets")
def retry_sheets():
    secret = os.getenv("CRON_SECRET", "")
    supplied = request.headers.get("Authorization", "")
    if len(secret) < 32 or not hmac.compare_digest(supplied, "Bearer " + secret):
        return _error(401, "unauthorized", "Unauthorized")
    try:
        owner = _backend()
        with owner.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Daily reconciliation also rolls the current-month wage summary.
            owner._queue_all_sheet_datasets(conn)
            conn.commit()
        batch = _dispatch_sheets(limit=len(SHEET_DATASETS))
    except Exception:
        return _error(503, "sheet_retry_unavailable", "Retry is temporarily unavailable")
    return _json_response(Response(200, {"ok": True, "configured": batch is not None,
                                     "batch": batch}))


@app.get("/api/receipts/<receipt_id>.pdf")
def receipt_download(receipt_id: str):
    if not re.fullmatch(r"[0-9a-f]{32}", receipt_id):
        return _error(404, "receipt_not_found", "Receipt not found")
    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        result = _backend().handle("GET", f"/api/receipts/{receipt_id}", None, headers, _client_ip())
    except (DatabaseError, ProtocolError, RuntimeError, ValueError):
        return _error(503, "receipt_unavailable", "The receipt is temporarily unavailable")
    if result.status != 200:
        return _json_response(result)
    receipt = result.body["receipt"]
    try:
        # ReportLab is only needed for receipt downloads; keeping it out of
        # the common import path shortens cold login/dashboard invocations.
        from receipt_pdf import render_receipt_pdf
        document = render_receipt_pdf(receipt)
    except (OSError, RuntimeError, ValueError):
        return _error(503, "receipt_unavailable", "The receipt is temporarily unavailable")
    response = make_response(document, 200)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = f'attachment; filename="{receipt["receipt_number"]}.pdf"'
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response.headers["Vary"] = "Cookie, Origin"
    return response


@app.route("/api/<path:api_path>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def api(api_path: str):
    if request.method in {"POST", "PUT"}:
        if not request.is_json:
            return _error(415, "unsupported_media_type", "Send application/json")
        try:
            payload = json.loads(request.get_data(cache=False))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _error(400, "invalid_json", "Invalid JSON")
        if not isinstance(payload, dict):
            return _error(400, "invalid_json", "A JSON object is required")
    else:
        payload = None

    target = request.path + ("?" + request.query_string.decode("ascii", errors="ignore") if request.query_string else "")
    headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        result = _backend().handle(request.method, target, payload, headers, _client_ip())
    except (DatabaseError, ProtocolError):
        return _error(503, "database_unavailable", "The service is temporarily unavailable")
    except (ValueError, RuntimeError):
        return _error(503, "service_unconfigured", "The service is not ready")
    if request.method in {"POST", "PUT"} and 200 <= result.status < 300 and target not in {
        "/api/auth/login", "/api/auth/logout"
    }:
        # The response is based on the already committed business update.
        # Provider or outbox failure must not turn a successful update into 500.
        sheet_batch = {"processed": 0, "synced": 0, "retrying": 0}
        try:
            sheet_batch = _dispatch_sheets(limit=len(SHEET_DATASETS)) or sheet_batch
        except Exception:
            pass
        if target == "/api/admin/sheet-sync/retry":
            # The admin retry button reports the attempted delivery, rather
            # than merely saying that another retry was queued.
            try:
                with _backend().connect() as conn:
                    result.body.update(_backend()._sheet_sync_status(conn))
            except Exception:
                pass
            result.body.update(sheet_batch)
        if target != "/api/admin/sheet-sync/retry":
            try:
                _dispatch_notifications(limit=2)
            except Exception:
                pass
    return _json_response(result)


@app.get("/")
def landing():
    return send_from_directory(PUBLIC_DIR, "index.html")


@app.get("/<path:asset>")
def public_asset(asset: str):
    dashboard_auth = None
    if asset in {"admin", "teacher", "student"}:
        try:
            access, dashboard_auth = _backend().static_auth(
                "/" + asset, request.headers.get("Cookie", ""), include_overview=True
            )
        except (DatabaseError, ProtocolError, RuntimeError, ValueError):
            return _error(503, "service_unavailable", "The service is temporarily unavailable")
        if access == "login":
            return redirect("/", code=303)
        if access == "forbidden":
            return _error(403, "forbidden", "This page is not available for your account")
        asset = "dashboard.html"
    if asset == "dashboard.html" and dashboard_auth is None:
        try:
            access, dashboard_auth = _backend().static_auth(
                "/dashboard.html", request.headers.get("Cookie", ""), include_overview=True
            )
        except (DatabaseError, ProtocolError, RuntimeError, ValueError):
            return _error(503, "service_unavailable", "The service is temporarily unavailable")
        if access == "login":
            return redirect("/", code=303)
    if asset == "dashboard.html":
        if dashboard_auth is None:
            return _error(403, "forbidden", "This page is not available for your account")
        bootstrap = json.dumps(dashboard_auth, ensure_ascii=True, separators=(",", ":"))
        # Keep user-controlled names and emails inside the inert JSON script.
        bootstrap = bootstrap.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        page = (PRIVATE_DIR / asset).read_text(encoding="utf-8")
        response = make_response(page.replace("__PP_AUTH_BOOTSTRAP__", bootstrap), 200)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Cookie"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        return response
    if asset not in {
        "index.html", "live.js", "live.css", "portal.css",
        "design.css", "physics-hero-gold.png", "physics-plus-logo-yellow-black.png",
        "physics-hero-gold.jpg", "physics-plus-logo-yellow-black.jpg", "physics-plus-favicon.png",
    }:
        return _error(404, "not_found", "Page not found")
    response = make_response(send_from_directory(PUBLIC_DIR, asset))
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Cache-Control"] = "no-store" if asset.endswith(".html") else "public, max-age=3600"
    return response

