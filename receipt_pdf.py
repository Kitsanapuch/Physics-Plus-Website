"""Render a confirmed Physics Plus payment as a Thai receipt PDF.

This is an ordinary payment acknowledgement (ใบสำคัญรับเงิน), not a tax invoice.
The caller must authorize access to the immutable receipt record before calling
``render_receipt_pdf``. No user supplied markup is interpreted.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


_FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
_FONT_REGULAR = "PhysicsPlusSarabun"
_FONT_BOLD = "PhysicsPlusSarabunBold"
_BANGKOK = ZoneInfo("Asia/Bangkok")
_INK = colors.HexColor("#20211E")
_MUTED = colors.HexColor("#6C6F6A")
_YELLOW = colors.HexColor("#F4C84B")
_LIGHT = colors.HexColor("#F8F6EF")
_BORDER = colors.HexColor("#E7E3D7")
_MONTHS = (
    "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
    "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม",
)
_METHODS = {
    "claim_verified": "โอนเงิน (ผู้ดูแลตรวจสอบแล้ว)",
    "bank_transfer": "โอนเงินผ่านธนาคาร",
    "transfer": "โอนเงินผ่านธนาคาร",
    "cash": "เงินสด",
    "card": "บัตร",
    "other": "อื่น ๆ",
}


def _register_fonts() -> None:
    if _FONT_REGULAR not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_FONT_REGULAR, str(_FONT_DIR / "Sarabun-Regular.ttf")))
    if _FONT_BOLD not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(_FONT_BOLD, str(_FONT_DIR / "Sarabun-Bold.ttf")))


def _value(receipt: Mapping[str, Any], key: str, *, limit: int = 180) -> str:
    value = receipt.get(key)
    if value is None:
        return "-"
    # Prevent control characters from disturbing the PDF text layout.
    text = " ".join(str(value).split())
    return text[:limit] if text else "-"


def _date(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, (int, float)) or (isinstance(value, str) and value.isdecimal()):
        dt = datetime.fromtimestamp(int(value), tz=timezone.utc)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return _value({"date": value}, "date", limit=40)
    else:
        return _value({"date": value}, "date", limit=40)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(_BANGKOK)
    return f"{local.day} {_MONTHS[local.month - 1]} {local.year + 543} เวลา {local:%H:%M} น."


def _wrap(text: str, font: str, size: float, max_width: float, max_lines: int = 3) -> list[str]:
    words = text.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        proposed = f"{current} {word}" if current else word
        if pdfmetrics.stringWidth(proposed, font, size) <= max_width:
            current = proposed
            continue
        if current:
            lines.append(current)
            current = ""
        while word and pdfmetrics.stringWidth(word, font, size) > max_width:
            cut = len(word)
            while cut > 1 and pdfmetrics.stringWidth(word[:cut], font, size) > max_width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and pdfmetrics.stringWidth(last + "…", font, size) > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines or ["-"]


def _label(doc: canvas.Canvas, x: float, y: float, text: str) -> None:
    doc.setFillColor(_MUTED)
    doc.setFont(_FONT_REGULAR, 10)
    doc.drawString(x, y, text)


def _text(doc: canvas.Canvas, x: float, y: float, text: str, *, bold: bool = False, size: float = 12) -> None:
    doc.setFillColor(_INK)
    doc.setFont(_FONT_BOLD if bold else _FONT_REGULAR, size)
    doc.drawString(x, y, text)


def render_receipt_pdf(receipt: Mapping[str, Any]) -> bytes:
    """Return an A4 PDF for one confirmed payment's immutable receipt snapshot.

    Expected keys: ``receipt_number``, ``issued_at``, ``paid_at``,
    ``student_name``, ``description``, ``amount_satang``, ``method``,
    ``reference``, and ``invoice_number``. ``course_title`` and ``course_code``
    identify the student's enrolled course when available. Timestamps can be
    Unix seconds or ISO 8601 strings. Every partial payment gets its own
    document and amount.
    """
    amount = int(receipt["amount_satang"])
    if amount <= 0:
        raise ValueError("Receipt amount must be positive")
    _register_fonts()

    stream = BytesIO()
    doc = canvas.Canvas(stream, pagesize=A4, pageCompression=1)
    doc.setTitle("ใบสำคัญรับเงิน - Physics Plus")
    doc.setAuthor("Physics Plus")
    doc.setSubject("Payment acknowledgement; not a tax invoice")
    width, height = A4
    left, right = 50, width - 50

    # Brand band and document heading.
    doc.setFillColor(_YELLOW)
    doc.rect(0, height - 13, width, 13, stroke=0, fill=1)
    doc.setFillColor(_INK)
    doc.roundRect(left, height - 101, 42, 42, radius=11, stroke=0, fill=1)
    doc.setFillColor(_YELLOW)
    doc.setFont(_FONT_BOLD, 17)
    doc.drawCentredString(left + 21, height - 86, "P+")
    _text(doc, left + 55, height - 76, "Physics Plus", bold=True, size=19)
    doc.setFillColor(_MUTED)
    doc.setFont(_FONT_REGULAR, 9.5)
    doc.drawString(left + 55, height - 95, "ร้านสอนพิเศษ")
    doc.setStrokeColor(_BORDER)
    doc.line(left, height - 121, right, height - 121)

    _text(doc, left, height - 164, "ใบสำคัญรับเงิน", bold=True, size=23)
    doc.setFillColor(_MUTED)
    doc.setFont(_FONT_REGULAR, 10)
    doc.drawString(left, height - 184, "PAYMENT RECEIPT")

    # Document identity. The issued/paid timestamps are separate because a
    # legacy payment may receive its receipt during migration.
    doc.setFillColor(_LIGHT)
    doc.roundRect(left, height - 273, width - 100, 68, radius=9, stroke=0, fill=1)
    _label(doc, left + 16, height - 225, "เลขที่เอกสาร")
    _text(doc, left + 16, height - 245, _value(receipt, "receipt_number", limit=64), bold=True, size=12)
    _label(doc, left + 245, height - 225, "วันที่ออกเอกสาร")
    _text(doc, left + 245, height - 245, _date(receipt.get("issued_at")), size=10.3)

    y = height - 315
    _label(doc, left, y, "ได้รับจาก")
    student_name = _value(receipt, "student_name", limit=120)
    name_size = 11 if len(student_name) > 65 else 15
    for i, line in enumerate(_wrap(student_name, _FONT_BOLD, name_size, right - left, 2)):
        _text(doc, left, y - 22 - i * 18, line, bold=True, size=name_size)
    doc.setStrokeColor(_BORDER)
    doc.line(left, y - 55, right, y - 55)

    y -= 90
    _label(doc, left, y, "รายการตามคำสั่งสมัครเรียน")
    description = _value(receipt, "description", limit=250)
    course_title = _value(receipt, "course_title", limit=160)
    course_code = _value(receipt, "course_code", limit=40)
    item_lines: list[tuple[str, bool]] = []
    if course_title != "-":
        item_lines.extend((line, True) for line in _wrap(course_title, _FONT_BOLD, 12, right - left, 2))
        if course_code != "-":
            item_lines.append((f"รหัสวิชา {course_code}", False))
        if description != "-" and description != course_title:
            item_lines.extend((line, False) for line in _wrap(description, _FONT_REGULAR, 10.5, right - left, 3))
    else:
        item_lines.extend((line, False) for line in _wrap(description, _FONT_REGULAR, 12, right - left, 4))
    for i, (line, bold) in enumerate(item_lines):
        _text(doc, left, y - 22 - i * 16, line, bold=bold, size=12 if bold else 10.5)

    y -= 100 + max(0, len(item_lines) - 4) * 10
    doc.setFillColor(_LIGHT)
    doc.roundRect(left, y - 64, width - 100, 70, radius=9, stroke=0, fill=1)
    _label(doc, left + 16, y - 16, "จำนวนเงินที่รับ")
    doc.setFillColor(_INK)
    doc.setFont(_FONT_BOLD, 24)
    doc.drawRightString(right - 16, y - 38, f"{amount / 100:,.2f} บาท")

    y -= 112
    _label(doc, left, y, "วันที่รับชำระ")
    _text(doc, left, y - 20, _date(receipt.get("paid_at")), size=11)
    _label(doc, left + 260, y, "วิธีชำระเงิน")
    method = _value(receipt, "method", limit=50)
    _text(doc, left + 260, y - 20, _METHODS.get(method, method), size=11)

    y -= 62
    _label(doc, left, y, "อ้างอิงใบแจ้งชำระ")
    _text(doc, left, y - 20, _value(receipt, "invoice_number", limit=64), size=11)
    _label(doc, left + 260, y, "เลขอ้างอิงการชำระเงิน")
    reference = _value(receipt, "reference", limit=120)
    for i, line in enumerate(_wrap(reference, _FONT_REGULAR, 10.5, right - (left + 260), 3)):
        _text(doc, left + 260, y - 20 - i * 15, line, size=10.5)

    # An acknowledgement only. Do not imply a signature or tax status.
    doc.setStrokeColor(_BORDER)
    doc.line(left, 70, right, 70)
    doc.setFillColor(_MUTED)
    doc.setFont(_FONT_REGULAR, 10)
    doc.drawString(left, 50, "ออกโดยระบบ Physics Plus หลังผู้ดูแลยืนยันการรับชำระเงิน")
    doc.drawString(left, 32, "เอกสารนี้เป็นใบสำคัญรับเงิน ไม่ใช่ใบกำกับภาษี")
    doc.setFillColor(_YELLOW)
    doc.roundRect(right - 91, 26, 91, 25, radius=6, stroke=0, fill=1)
    doc.setFillColor(_INK)
    doc.setFont(_FONT_BOLD, 9)
    doc.drawCentredString(right - 45.5, 34.5, "Physics Plus")

    doc.showPage()
    doc.save()
    return stream.getvalue()
