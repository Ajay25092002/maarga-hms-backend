
from __future__ import annotations

import os
import re

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from io import BytesIO
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import Response
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    LongTable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from Services.bill_query_service import get_bill_service

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = BASE_DIR / "assets"

BLACK = colors.HexColor("#111111")
GREY = colors.HexColor("#D0D0D0")
GRID = colors.HexColor("#999999")
LIGHT_GREY = colors.HexColor("#F2F2F2")
FOOTER = colors.HexColor("#555555")
CONTENT_WIDTH = A4[0] - 30 * mm


def _money(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _date_value(value: Any):
    if not value:
        return None
    if isinstance(value, (datetime, date)):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _date_text(value: Any, include_time: bool = False) -> str:
    parsed = _date_value(value)
    if not parsed:
        return str(value) if value else "-"
    return parsed.strftime(
        "%d-%m-%Y %H:%M" if include_time else "%d-%m-%Y"
    )


def _display(value: Any) -> str:
    return "-" if value is None or value == "" else str(value)


def _filename_part(value: Any) -> str:
    """Return a filesystem-safe filename part while preserving readable names."""
    text = str(value or "").strip()
    # A slash in the main bill number must become a hyphen in a filename.
    text = text.replace("/", "-").replace("\\", "-")
    text = re.sub(r'[<>:"|?*\x00-\x1f]', "-", text)
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return text or "Unknown"


def _download_filename(bill: dict, bill_type: str, bill_id: int) -> str:
    patient = bill.get("patient") if isinstance(bill.get("patient"), dict) else {}
    patient_name = (
        bill.get("patient_name")
        or patient.get("name")
        or patient.get("full_name")
        or "Unknown Patient"
    )
    # Always use the main bill_number, never ip_number or op_number.
    main_bill_number = bill.get("bill_number") or bill_id
    return (
        f"PT. {_filename_part(patient_name)} BILL {bill_type}-Bill-"
        f"{_filename_part(main_bill_number)}.pdf"
    )


def _calculate_age(date_of_birth):
    if not date_of_birth:
        return "-"

    try:
        if isinstance(date_of_birth, datetime):
            dob = date_of_birth.date()
        elif isinstance(date_of_birth, date):
            dob = date_of_birth
        else:
            dob = date.fromisoformat(str(date_of_birth)[:10])

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()

        if dob > today:
            return "-"

        age = today.year - dob.year
        if (today.month, today.day) < (dob.month, dob.day):
            age -= 1

        return age

    except (ValueError, TypeError):
        return "-"


def _p(value: Any, style: ParagraphStyle) -> Paragraph:
    text = _display(value)
    text = (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )
    return Paragraph(text, style)


def _styles():
    sample = getSampleStyleSheet()
    return {
        "hospital": ParagraphStyle(
            "BillHospital",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=14,
            textColor=BLACK,
            alignment=TA_LEFT,
        ),
        "address": ParagraphStyle(
            "BillAddress",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9,
            textColor=BLACK,
            alignment=TA_LEFT,
        ),
        "title": ParagraphStyle(
            "BillTitle",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=12,
            textColor=BLACK,
            alignment=TA_CENTER,
        ),
        "normal": ParagraphStyle(
            "BillNormal",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9,
            textColor=BLACK,
        ),
        "small": ParagraphStyle(
            "BillSmall",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=6.8,
            leading=8,
            textColor=BLACK,
        ),
        "bold": ParagraphStyle(
            "BillBold",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9,
            textColor=BLACK,
        ),
        "right": ParagraphStyle(
            "BillRight",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=7.3,
            leading=9,
            textColor=BLACK,
            alignment=TA_RIGHT,
        ),
        "center": ParagraphStyle(
            "BillCenter",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=7,
            leading=8,
            textColor=BLACK,
            alignment=TA_CENTER,
        ),
        "table_head": ParagraphStyle(
            "BillTableHead",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=8,
            textColor=BLACK,
        ),
        "footer": ParagraphStyle(
            "BillFooter",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=6.5,
            leading=8,
            textColor=FOOTER,
        ),
    }


def _header_footer(canvas, doc, bill_type: str):
    """Draw the existing OP/IP header and footer on every page."""
    canvas.saveState()
    page_w, page_h = A4
    left = 15 * mm
    right = page_w - 15 * mm
    logo_path = ASSETS_DIR / "maarga_logo.png"

    if bill_type == "OP":
        canvas.setFillColor(BLACK)
        canvas.setFont("Helvetica-Bold", 12)
        canvas.drawString(
            left,
            page_h - 12 * mm,
            "MAARGA MINDCARE PRIVATE LIMITED",
        )
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(
            left,
            page_h - 20 * mm,
            "A-14, Golfcourse Ext road, Block A, Sushant Lok III, "
            "Sector 57, Gurugram, Haryana, 122011",
        )

        if logo_path.exists():
            canvas.drawImage(
                str(logo_path),
                right - 22 * mm,
                page_h - 24 * mm,
                width=20 * mm,
                height=20 * mm,
                preserveAspectRatio=True,
                anchor="c",
                mask="auto",
            )

        canvas.setLineWidth(0.6)
        canvas.setStrokeColor(GRID)
        canvas.line(left, page_h - 27 * mm, right, page_h - 27 * mm)
        canvas.line(left, 15 * mm, right, 15 * mm)
        canvas.setFillColor(FOOTER)
        canvas.setFont("Helvetica", 6.5)
        canvas.drawString(left, 10 * mm, "Printed from MAARGA Mindcare")
        canvas.drawRightString(right, 10 * mm, f"Page {doc.page}")
        canvas.setFont("Helvetica", 6)
        canvas.drawString(
            left,
            6 * mm,
            "Website: www.maargamindcare.com  |  Email: maarga@maargamindcare.com",
        )
    else:
        if logo_path.exists():
            canvas.drawImage(
                str(logo_path),
                left,
                page_h - 26 * mm,
                width=18 * mm,
                height=17 * mm,
                preserveAspectRatio=True,
                anchor="c",
                mask="auto",
            )

        text_x = left + 23 * mm
        canvas.setFillColor(BLACK)
        canvas.setFont("Helvetica", 14)
        canvas.drawString(
            text_x,
            page_h - 13 * mm,
            "MAARGA MINDCARE - GURUGRAM ( 2MG )",
        )
        canvas.setFont("Helvetica", 8)
        canvas.drawString(
            text_x,
            page_h - 19 * mm,
            "A-14, Golfcourse Ext road, Block A, Sushant Lok III, Sector 57, Gurugram, Haryana, 122011",
        )
        canvas.setStrokeColor(BLACK)
        canvas.setLineWidth(0.8)
        canvas.line(left, page_h - 28 * mm, right, page_h - 28 * mm)
        canvas.setLineWidth(0.6)
        canvas.line(left, 14 * mm, right, 14 * mm)
        canvas.setFillColor(FOOTER)
        canvas.setFont("Helvetica", 7)
        canvas.drawString(
            left,
            10 * mm,
            "Powered By MEDNET for MAARGA MINDCARE - GURUGRAM ( 2MG )",
        )
        canvas.drawRightString(right, 10 * mm, f"Page {doc.page}")
        canvas.drawRightString(right, 6 * mm, "www.mednetlabs.com")

    canvas.restoreState()


def _table_style():
    return TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.45, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2.3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.3),
    ])


def _calculate_totals(
    bill: dict,
    items: list[dict],
    payments: list[dict],
    refunds: list[dict],
):
    # Keep the bill's original charge totals unchanged. Refunds affect net
    # retained collections, while outstanding remains based on original payments.
    subtotal = _money(bill.get("subtotal"))
    if not subtotal:
        subtotal = sum(
            _money(item.get("amount", _money(item.get("rate")) * _money(item.get("quantity") or 1)))
            for item in items
        )

    item_discount = sum(_money(item.get("discount_amount")) for item in items)
    bill_discount = _money(bill.get("discount_amount"))
    discount_total = item_discount + bill_discount
    net_total = _money(bill.get("total_amount"))

    # Avoid stale bill.amount_paid overriding the actual payment history.
    total_payments = sum(_money(payment.get("amount")) for payment in payments)
    total_refunds = sum(_money(refund.get("refund_amount")) for refund in refunds)
    net_paid = max(0.0, total_payments - total_refunds)
    # Outstanding is based on original payments, not net paid after refunds.
    balance = max(0.0, net_total - total_payments)

    return subtotal, discount_total, net_total, total_payments, total_refunds, net_paid, balance


def _database_connection():
    """Use the same standard PostgreSQL environment variables as the backend."""
    for env_name in (
        "DATABASE_URL",
        "DATABASE_CONNECTION_STRING",
        "POSTGRES_CONNECTION_STRING",
        "POSTGRES_URL",
    ):
        value = os.getenv(env_name)
        if value and value.strip():
            return psycopg2.connect(value.strip(), connect_timeout=10, options="-c timezone=Asia/Kolkata")
    raise RuntimeError(
        "PostgreSQL connection string is not configured. "
        "Set DATABASE_URL or a supported PostgreSQL connection variable."
    )


def _get_bill_refunds(bill_id: int) -> list[dict]:
    """Fetch refund transactions separately so payments are never duplicated."""
    connection = _database_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    pr.id,
                    pr.refund_number,
                    pr.bill_id,
                    pr.refund_amount,
                    pr.refund_method,
                    pr.reason,
                    pr.notes,
                    pr.created_by,
                    pr.created_at,
                    NULL::varchar AS receipt_number,
                    NULL::varchar AS original_payment_method
                FROM public.payment_refunds pr
                WHERE pr.bill_id = %s
                ORDER BY pr.created_at ASC, pr.id ASC
                """,
                (bill_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()



# These are the eight services auto-populated by the IP billing screen.
# Matching is case-insensitive and also tolerates common spelling variations.
IP_AUTO_SERVICE_NAMES = {
    "ROOM RENT",
    "RECREATION ACTVITIES/THERAPY",
    "PSYCHIATRIC CONSULTATION/TREATMENT",
    "DOCTOR CONSULTATION",
    "NURSING CHARGES",
    "PSYCHOLOGIST COUNSELLING/TREATMENT",
    "HOLISTIC THERAPIES",
    "DIET & FOOD",
}


def _normal_name(value: Any) -> str:
    return " ".join(str(value or "").strip().upper().split())


def _room_periods(bill: dict) -> list[dict]:
    """Normalize room-history periods and close an open current stay at today's date."""
    history = bill.get("room_history") or bill.get("room_history_timeline") or []
    periods = []

    for record in history:
        start = _date_value(
            record.get("start_date") or record.get("start_at")
            or record.get("admission_date") or record.get("from_date")
            or record.get("assigned_at") or record.get("created_at")
        )
        end = _date_value(
            record.get("end_date") or record.get("end_at")
            or record.get("discharge_date") or record.get("to_date")
            or record.get("released_at")
        )
        if not start:
            continue
        periods.append({
            "start": start,
            "end": end,
            "room_type_name": record.get("room_type_name") or record.get("room_name")
                or record.get("room_type") or bill.get("room_type_name") or "",
            "bed_number": record.get("bed_number") or record.get("bed_no")
                or record.get("bed_name") or bill.get("bed_number") or "",
            "room_type_id": record.get("room_type_id") or record.get("room_id"),
        })

    periods.sort(key=lambda period: period["start"])
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    discharge = _date_value(bill.get("discharge_date"))

    for index, period in enumerate(periods):
        if period["end"] is None:
            if index + 1 < len(periods):
                next_start = periods[index + 1]["start"]
                period["end"] = datetime.combine(
                    next_start.date() - timedelta(days=1), datetime.max.time()
                )
            elif discharge:
                period["end"] = discharge
            else:
                period["end"] = datetime.combine(today, datetime.min.time())

    periods = [
        period for period in periods
        if period["end"] and period["end"].date() >= period["start"].date()
    ]

    # Fallback when room history is unavailable.
    if not periods:
        start = _date_value(
            bill.get("admission_date") or bill.get("admitted_at")
            or bill.get("admission_datetime") or bill.get("created_at")
            or bill.get("bill_date")
        )
        if start and start.date() <= today:
            periods.append({
                "start": start,
                "end": discharge or datetime.combine(today, datetime.min.time()),
                "room_type_name": (
                    bill.get("room_type_name") or bill.get("current_room_type_name")
                    or bill.get("room_type") or bill.get("current_room_type") or ""
                ),
                "bed_number": (
                    bill.get("bed_number") or bill.get("bed_no")
                    or bill.get("current_bed_number") or ""
                ),
                "room_type_id": bill.get("room_type_id") or bill.get("room_id"),
            })

    return periods


def _period_label(period: dict) -> str:
    start = _date_text(period["start"])
    end = _date_text(period["end"])
    room = str(period.get("room_type_name") or "").strip()
    bed = str(period.get("bed_number") or "").strip()
    room_bed = "/".join(part for part in (room, bed) if part)
    suffix = f" {room_bed}" if room_bed else ""
    return f"({start} to {end}{suffix})"


def _ip_display_items(bill: dict, items: list[dict]) -> list[dict]:
    """Decorate existing IP bill items with room-period context without adding rows.

    The saved bill items are the financial source of truth. Room history is used
    only to find the relevant stay period for the service date. This function
    must return exactly one display row for each saved bill item and must never
    recalculate quantity, rate, amount, discount, GST, or net amount.
    """
    periods = _room_periods(bill)
    display_items: list[dict] = []

    for item in items:
        row = dict(item)
        name = (
            item.get("service_name")
            or item.get("name")
            or item.get("description")
            or item.get("service_code")
            or ""
        )
        service_date = _date_value(item.get("service_date") or item.get("created_at"))

        # The bill item itself is authoritative for its assigned room/bed.
        item_room_name = (
            item.get("room_type_name")
            or item.get("room_name")
            or item.get("room_type")
            or ""
        )
        item_bed = (
            item.get("bed_number")
            or item.get("bed_no")
            or item.get("bed_name")
            or ""
        )
        item_room_id = item.get("room_type_id") or item.get("room_id")

        # Match the saved item room/bed to room history FIRST. A room-rent
        # item may intentionally be entered with a service date before the
        # room assignment became active (for example, a charge entered on the
        # 23rd for a room assigned on the 24th). In that case, the item's saved
        # room/bed identifies the correct period, and its period dates should
        # still be shown. Service date is only a secondary disambiguator.
        def period_matches_item(period: dict) -> bool:
            room_id_matches = (
                item_room_id is not None
                and period.get("room_type_id") is not None
                and str(item_room_id) == str(period.get("room_type_id"))
            )
            room_name_matches = (
                bool(item_room_name)
                and bool(period.get("room_type_name"))
                and _normal_name(item_room_name) == _normal_name(period.get("room_type_name"))
            )
            bed_matches = (
                bool(item_bed)
                and bool(period.get("bed_number"))
                and str(item_bed).strip() == str(period.get("bed_number")).strip()
            )
            # Prefer the strongest available match. If a room name is present,
            # require the bed to match too when the item has a bed number.
            return (
                room_id_matches
                or (room_name_matches and (not item_bed or bed_matches))
                or (bed_matches and not item_room_name)
            )

        item_matching_periods = [period for period in periods if period_matches_item(period)]
        selected_period = None
        if item_matching_periods:
            # If the same room/bed has multiple historical periods, choose the
            # one containing service_date where possible; otherwise choose the
            # latest matching period. This preserves the item's stored room and
            # never creates extra bill rows.
            date_matching_item_periods = [
                period for period in item_matching_periods
                if service_date
                and period["start"].date() <= service_date.date() <= period["end"].date()
            ]
            selected_period = (
                date_matching_item_periods[-1]
                if date_matching_item_periods
                else item_matching_periods[-1]
            )
        elif service_date:
            # Only fall back to service-date matching if the item has no
            # usable room/bed match in the history.
            date_matching_periods = [
                period for period in periods
                if period["start"].date() <= service_date.date() <= period["end"].date()
            ]
            if date_matching_periods:
                selected_period = date_matching_periods[-1]

        # If service date is absent, room/bed fields still come from the bill
        # item. Do not infer a date range that could misrepresent the charge.
        room_name = item_room_name or (selected_period or {}).get("room_type_name") or ""
        bed_number = item_bed or (selected_period or {}).get("bed_number") or ""

        # Preserve the original description if this item already has a period
        # label, avoiding duplicate labels if this helper is called more than once.
        clean_name = name
        if selected_period:
            period_label = _period_label(selected_period)
            if period_label not in clean_name:
                room_bed = "/".join(
                    part for part in (str(room_name).strip(), str(bed_number).strip()) if part
                )
                start_text = _date_text(selected_period["start"])
                end_text = _date_text(selected_period["end"])
                suffix = f" {room_bed}" if room_bed else ""
                clean_name = f"{name} ({start_text} to {end_text}{suffix})"
        elif room_name or bed_number:
            # If history cannot be matched, show the item-specific room/bed
            # without fabricating a room-period date range.
            room_bed = "/".join(
                part for part in (str(room_name).strip(), str(bed_number).strip()) if part
            )
            suffix = f" ({room_bed})"
            if suffix not in clean_name:
                clean_name = f"{name}{suffix}"

        if name:
            row["service_name"] = clean_name
            row["_ip_original_service_name"] = name
        if room_name:
            row["room_type_name"] = room_name
        if bed_number:
            row["bed_number"] = bed_number
        if selected_period:
            row["room_history_start"] = selected_period["start"]
            row["room_history_end"] = selected_period["end"]

        # Do not alter any financial fields or quantity here.
        display_items.append(row)

    return display_items


def _aggregate_ip_auto_items(items: list[dict]) -> list[dict]:
    """Aggregate only the eight automated IP services for PDF display.

    Saved bill items are never modified. Rows are combined only when their
    service, room-history period, room/bed, rate, and financial calculation
    fields match. All non-automated services remain individual rows.
    """
    auto_names = {_normal_name(name) for name in IP_AUTO_SERVICE_NAMES}
    output: list[dict] = []
    grouped: dict[tuple, dict] = {}
    sum_fields = (
        "quantity", "amount", "discount_amount", "net_amount", "gst_amount",
    )

    for item in items:
        original_name = item.get("_ip_original_service_name") or (
            item.get("service_name") or item.get("name")
            or item.get("description") or item.get("service_code") or ""
        )
        normalized_name = _normal_name(original_name)

        if normalized_name not in auto_names:
            output.append(item)
            continue

        period_start = item.get("room_history_start")
        period_end = item.get("room_history_end")
        # If no room period was resolved, do not merge unrelated entries.
        if not period_start or not period_end:
            output.append(item)
            continue

        key = (
            normalized_name,
            str(period_start),
            str(period_end),
            str(item.get("room_type_id") or item.get("room_id") or ""),
            _normal_name(item.get("room_type_name") or item.get("room_name") or ""),
            str(item.get("bed_number") or item.get("bed_no") or ""),
            _money(item.get("rate")),
            _money(item.get("discount_rate")),
            _money(item.get("gst_rate")),
            _money(item.get("discount_percentage")),
            _normal_name(item.get("discount_type") or ""),
        )

        if key not in grouped:
            merged = dict(item)
            merged["service_name"] = (
                f"{original_name} {_period_label({
                    'start': period_start,
                    'end': period_end,
                    'room_type_name': item.get('room_type_name') or '',
                    'bed_number': item.get('bed_number') or '',
                })}"
            )
            # The displayed Service Date must be the earliest actual bill-item
            # service date in this aggregated row, not the room-history start.
            # Room-history dates remain in the service description separately.
            actual_service_date = _date_value(item.get("service_date") or item.get("created_at"))
            merged["service_date"] = actual_service_date or item.get("service_date") or item.get("created_at")
            merged["quantity"] = _money(item.get("quantity") or 1)
            for field in ("amount", "discount_amount", "net_amount", "gst_amount"):
                if item.get(field) is not None:
                    merged[field] = _money(item.get(field))
            grouped[key] = merged
            output.append(merged)
        else:
            merged = grouped[key]
            # Keep the first actual service date among all items combined into
            # this row, including cases where it precedes the room-history start.
            current_service_date = _date_value(merged.get("service_date") or merged.get("created_at"))
            item_service_date = _date_value(item.get("service_date") or item.get("created_at"))
            if item_service_date and (
                not current_service_date or item_service_date < current_service_date
            ):
                merged["service_date"] = item_service_date
            merged["quantity"] = _money(merged.get("quantity") or 0) + _money(item.get("quantity") or 1)
            for field in ("amount", "discount_amount", "net_amount", "gst_amount"):
                if merged.get(field) is not None or item.get(field) is not None:
                    merged[field] = _money(merged.get(field)) + _money(item.get(field))

    # Strip private helper metadata before rendering.
    for item in output:
        item.pop("_ip_original_service_name", None)
    return output


def _charge_table(items: list[dict], styles, op: bool):

    if op:
        widths = [
            12 * mm,
            CONTENT_WIDTH - 12 * mm - 24 * mm - 14 * mm - 22 * mm - 20 * mm - 26 * mm,
            24 * mm,
            14 * mm,
            22 * mm,
            20 * mm,
            26 * mm,
        ]

        rows = [[
            _p("Sl No", styles["table_head"]),
            _p("Service Name", styles["table_head"]),
            _p("Rate", styles["table_head"]),
            _p("Qty", styles["table_head"]),
            _p("Amount", styles["table_head"]),
            _p("Discount", styles["table_head"]),
            _p("Net Amount", styles["table_head"]),
        ]]

        for index, item in enumerate(items, start=1):
            amount = _money(
                item.get(
                    "amount",
                    _money(item.get("rate")) * _money(item.get("quantity") or 1),
                )
            )
            discount = _money(item.get("discount_amount"))
            net_amount = _money(item.get("net_amount", amount - discount))

            rows.append([
                _p(index, styles["normal"]),
                _p(
                    item.get("service_name")
                    or item.get("name")
                    or item.get("description")
                    or item.get("service_code")
                    or "-",
                    styles["normal"],
                ),
                _p(f"{_money(item.get('rate')):,.2f}", styles["right"]),
                _p(item.get("quantity") or 1, styles["right"]),
                _p(f"{amount:,.2f}", styles["right"]),
                _p(f"{discount:,.2f}", styles["right"]),
                _p(f"{net_amount:,.2f}", styles["right"]),
            ])

        if len(rows) == 1:
            rows.append([
                _p("No charge items", styles["normal"]),
                "", "", "", "", "", "",
            ])

        table = LongTable(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
        table.setStyle(_table_style())
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), GREY),
            ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ]))
        return table


    
    widths = [
        12 * mm,
        23 * mm,
        52 * mm,
        17 * mm,
        13 * mm,
        21 * mm,
        17 * mm,
        15 * mm,
        CONTENT_WIDTH - 170 * mm,
    ]


    grouped: dict[str, list[dict]] = {}
    for item in items:
        # bill_items uses service_group_name (and service_group_id), not
        # `category`. Prefer the persisted group name so charges remain under
        # their actual configured service groups in the IP PDF.
        category = (
            item.get("service_group_name")
            or item.get("service_group")
            or item.get("category")
        )
        category = str(category or "OTHER CHARGES").strip()
        if not category:
            category = "OTHER CHARGES"
        grouped.setdefault(category, []).append(item)

    if not grouped:
        grouped["OTHER CHARGES"] = []

    
    rows = [[
        _p("No.", styles["table_head"]),
        _p("Date", styles["table_head"]),
        _p("Service Name", styles["table_head"]),
        _p("Rate", styles["table_head"]),
        _p("Qty.", styles["table_head"]),
        _p("Amount", styles["table_head"]),
        _p("Disc", styles["table_head"]),
        _p("Net Amt.", styles["table_head"]),
        _p("GST", styles["table_head"]),
    ]]


    category_header_rows = []
    line_no = 0

    for category, category_items in grouped.items():
        
        category_total = sum(
            _money(
                item.get(
                    "net_amount",
                    _money(item.get("amount"))
                    - _money(item.get("discount_amount")),
                )
            )
            + _money(item.get("gst_amount"))
            for item in category_items
        )


        category_header_rows.append(len(rows))
        
        rows.append([
            _p(category.upper(), styles["normal"]),
            "",
            "",
            "",
            "",
            _p(f"Total Rs. {category_total:,.2f}/-", styles["right"]),
            "",
            "",
            "",
        ])


        for item in category_items:
            line_no += 1
            amount = _money(
                item.get(
                    "amount",
                    _money(item.get("rate")) * _money(item.get("quantity")),
                )
            )
            discount = _money(item.get("discount_amount"))
            net = _money(item.get("net_amount", amount - discount))
            gst = _money(item.get("gst_amount"))
            service_name = (
                item.get("service_name")
                or item.get("name")
                or item.get("description")
                or item.get("service_code")
                or "-"
            )
            service_date = _date_text(item.get("service_date") or item.get("created_at"))

            
            rows.append([
                _p(line_no, styles["small"]),
                _p(service_date, styles["small"]),
                _p(service_name, styles["small"]),
                _p(f"{_money(item.get('rate')):,.2f}", styles["right"]),
                _p(item.get("quantity") or 1, styles["right"]),
                _p(f"{amount:,.2f}", styles["right"]),
                _p(f"{discount:,.2f}", styles["right"]),
                _p(f"{net:,.2f}", styles["right"]),
                _p(f"{gst:,.2f}", styles["right"]),
            ])


    table = LongTable(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(_table_style())
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), GREY),
    ]))

    for row_index in category_header_rows:
        table.setStyle(TableStyle([
            ("SPAN", (0, row_index), (4, row_index)),
            ("SPAN", (5, row_index), (8, row_index)),
            ("BACKGROUND", (0, row_index), (-1, row_index), LIGHT_GREY),
        ]))

    return table


def _transaction_table(records: list[dict], styles, op: bool, is_refund: bool = False):
    if op:
        widths = [
            CONTENT_WIDTH * 0.23,
            CONTENT_WIDTH * 0.17,
            CONTENT_WIDTH * 0.17,
            CONTENT_WIDTH * 0.23,
            CONTENT_WIDTH * 0.20,
        ]
        headings = (
            ["Refund No", "Date", "Refund Amount", "Refund Method", "Reason / Notes"]
            if is_refund
            else ["Receipt No", "Date", "Payment Amount", "Payment Method", "Status / Notes"]
        )
    else:
        widths = [
            29 * mm,
            31 * mm,
            28 * mm,
            35 * mm,
            CONTENT_WIDTH - 123 * mm,
        ]
        headings = (
            ["Refund No", "Date / Time", "Method", "Reason", "Refund Amount"]
            if is_refund
            else ["Receipt No", "Date / Time", "Method", "Notes", "Payment Amount"]
        )

    rows = [[_p(heading, styles["table_head"]) for heading in headings]]
    for record in records:
        amount_key = "refund_amount" if is_refund else "amount"
        amount = f"{_money(record.get(amount_key)):,.2f}"
        if is_refund:
            reference = record.get("refund_number") or record.get("id") or "-"
            when = record.get("created_at")
            method = record.get("refund_method") or "-"
            reason = record.get("reason") or record.get("notes") or "-"
            notes = record.get("notes") or ""
        else:
            reference = record.get("reference_number") or record.get("receipt_number") or record.get("id") or "-"
            when = record.get("payment_date")
            method = record.get("payment_method") or "-"
            reason = record.get("notes") or "-"
            notes = record.get("notes") or ""
        if op:
            rows.append([
                _p(reference, styles["small"]),
                _p(_date_text(when, True), styles["small"]),
                _p(amount, styles["right"]),
                _p(method, styles["small"]),
                _p(reason if is_refund else (notes or record.get("status") or "Received"), styles["small"]),
            ])
        else:
            rows.append([
                _p(reference, styles["small"]),
                _p(_date_text(when, True), styles["small"]),
                _p(method, styles["small"]),
                _p(reason, styles["small"]),
                _p(amount, styles["right"]),
            ])

    if len(rows) == 1:
        empty = "No refund records found." if is_refund else "No payment records found."
        rows.append([_p(empty, styles["small"]), "", "", "", ""])

    table = LongTable(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(_table_style())
    table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), GREY)]))
    return table



def _meta_row(
    label1: Any,
    value1: Any,
    label2: Any,
    value2: Any,
    label3: Any,
    value3: Any,
    styles: dict,
) -> Table:
    pairs = [
        (label1, value1),
        (label2, value2),
        (label3, value3),
    ]

    row = []
    col_widths = []

    available_width = CONTENT_WIDTH
    label_width = 20 * mm
    value_width = 35 * mm

    for label, value in pairs:
        if label and value not in ("", None):
            row.extend([
                _p(label, styles["bold"]),
                _p(value, styles["normal"]),
            ])
            col_widths.extend([label_width, value_width])

    if not row:
        return Table([[""]], colWidths=[available_width])

    # Expand the final value column to use all remaining space.
    col_widths[-1] = available_width - sum(col_widths[:-1])

    table = Table(
        [row],
        colWidths=col_widths,
        hAlign="LEFT",
    )
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))

    return table



def _generate_bill_pdf(bill_id: int, expected_type: str) -> Response:
    bill = get_bill_service(bill_id)
    actual_type = str(bill.get("bill_type", "")).strip().upper()

    if actual_type != expected_type:
        raise HTTPException(
            status_code=404,
            detail=f"{expected_type} bill not found.",
        )

    items = bill.get("items") or []
    payments = bill.get("payments") or []
    # Prefer refunds supplied by the bill service if available; otherwise
    # query the refund table directly for this bill.
    refunds = bill.get("refunds") or bill.get("refund_history") or _get_bill_refunds(bill_id)

    (
        subtotal,
        discount_total,
        net_total,
        total_payments,
        total_refunds,
        net_paid,
        balance,
    ) = _calculate_totals(bill, items, payments, refunds)

    styles = _styles()
    is_op = expected_type == "OP"
    story = []

    if is_op:
        story.extend([
            Spacer(1, 1 * mm),
            Table([[
                _p(f"UHID : {_display(bill.get('patient_id'))}", styles["bold"]),
                _p("OUTPATIENT BILL", styles["title"]),
                _p(
                    f"OP No : {_display(bill.get('bill_number') or bill.get('id'))}",
                    styles["right"],
                ),
            ]], colWidths=[
                CONTENT_WIDTH * 0.30,
                CONTENT_WIDTH * 0.40,
                CONTENT_WIDTH * 0.30,
            ]),
            Spacer(1, 1.5 * mm),
        ])

        info_rows = [
            [
                _p(
                    f"Patient : {_display(bill.get('patient_name') or bill.get('name'))}",
                    styles["normal"],
                ),
                _p(
                    f"Bill No : {_display(bill.get('bill_number') or bill.get('id'))}",
                    styles["normal"],
                ),
            ],
            [
                _p(
                    f"Age / Gender : {_display(_calculate_age(bill.get('date_of_birth')))}, {_display(bill.get('gender'))}",
                    styles["normal"],
                ),
                _p(
                    f"Bill Date : {_date_text(bill.get('bill_date'), True)}",
                    styles["normal"],
                ),
            ],
            [
                _p(
                    f"Doctor : {_display(bill.get('doctor_name'))}",
                    styles["normal"],
                ),
                _p(
                    f"Appointment Date : {_date_text(bill.get('appointment_date') or bill.get('bill_date'))}",
                    styles["normal"],
                ),
            ],
            [
                _p(
                    f"Psychologist : {_display(bill.get('psychologist_name'))}",
                    styles["normal"],
                ),
                _p(
                    f"Phone : {_display(bill.get('mobile'))}",
                    styles["normal"],
                ),
            ],
            [
                _p(
                    f"Address : {_display(bill.get('address'))}",
                    styles["normal"],
                ),
                _p(
                    f"Billed By : {_display(bill.get('created_by_name') or '-')}",
                    styles["normal"],
                )
            ],
        ]

        info = Table(
            info_rows,
            colWidths=[CONTENT_WIDTH * 0.68, CONTENT_WIDTH * 0.32],
        )
        info.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 1),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 1.3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.3),
        ]))

        story.extend([
            info,
            Spacer(1, 1.5 * mm),
            _charge_table(items, styles, True),
            Spacer(1, 1.5 * mm),
        ])

        gst_total = sum(_money(item.get("gst_amount")) for item in items)
        totals = Table([
            [_p("Subtotal :", styles["normal"]), _p(f"{subtotal:,.2f}", styles["right"])],
            [_p("Discount :", styles["normal"]), _p(f"{discount_total:,.2f}", styles["right"])],
            [_p("GST :", styles["normal"]), _p(f"{gst_total:,.2f}", styles["right"])],
            [_p("Bill Total :", styles["bold"]), _p(f"{net_total:,.2f}", styles["right"])],
            [_p("Total Payments :", styles["normal"]), _p(f"{total_payments:,.2f}", styles["right"])],
            [_p("Total Refunds :", styles["normal"]), _p(f"{total_refunds:,.2f}", styles["right"])],
            [_p("Net Paid :", styles["bold"]), _p(f"{net_paid:,.2f}", styles["right"])],
            [_p("Outstanding Balance :", styles["bold"]), _p(f"{balance:,.2f}", styles["right"])],
        ], colWidths=[CONTENT_WIDTH - 38 * mm, 38 * mm], hAlign="RIGHT")

        totals.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 1.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
        ]))

        story.extend([
            totals,
            Spacer(1, 3 * mm),
            *(
                [
                    _p("PAYMENT HISTORY", styles["bold"]),
                    Spacer(1, 1 * mm),
                    _transaction_table(payments, styles, True, False),
                    Spacer(1, 2 * mm),
                ]
                if payments else []
            ),
            *(
                [
                    _p("REFUND HISTORY", styles["bold"]),
                    Spacer(1, 1 * mm),
                    _transaction_table(refunds, styles, True, True),
                    Spacer(1, 4 * mm),
                ]
                if refunds else []
            ),
            _p(
                f"Printed By : {_display(bill.get('printed_by') or 'System')} "
                f"Prepared By : {_display(bill.get('prepared_by') or 'System')} "
                f"Print Date : {_date_text(datetime.now(ZoneInfo("Asia/Kolkata")), True)}",
                styles["footer"],
            ),
            Spacer(1, 1 * mm),
            _p(
                "Authorized Signature: _______________________________________________",
                styles["right"],
            ),
        ])

        filename = _download_filename(bill, "OP", bill_id)

    else:
        story.extend([
            Spacer(1, 1 * mm),
            _p(
                "Bill",
                ParagraphStyle(
                    "IPTitle",
                    parent=styles["title"],
                    fontName="Helvetica",
                    fontSize=9,
                ),
            ),
            Spacer(1, 2 * mm),
        ])

        patient = Table([
            [
                _p(
                    f"{_display(bill.get('patient_name') or bill.get('name'))} "
                    f"[{_display(bill.get('patient_id'))}] ",
                    styles["normal"],
                ),
                _p(
                    f"Age /Gender: {_display(_calculate_age(bill.get('date_of_birth')))} Yr / {_display(bill.get('gender'))}",
                    styles["right"],
                ),
            ],
            [
                _p(f"Address : {_display(bill.get('address'))}", styles["normal"]),
                _p(f"Contact No. : {_display(bill.get('mobile'))}", styles["right"]),
            ],
        ], colWidths=[115 * mm, CONTENT_WIDTH - 115 * mm])

        patient.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 1.3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1.3),
        ]))
        story.append(patient)

        dashed = Table([[""]], colWidths=[CONTENT_WIDTH], rowHeights=[1])
        dashed.setStyle(TableStyle([
            ("LINEBELOW", (0, 0), (-1, -1), 0.8, BLACK, None, (4, 3)),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))

        story.extend([
            Spacer(1, 1 * mm),
            dashed,
            _meta_row(
                "Bill No.",
                bill.get("bill_number") or bill_id,
                "IP No.",
                bill.get("ip_number") or "-",
                "Bill Date",
                _date_text(bill.get("bill_date")),
                styles,
            ),
            _meta_row(
                "Department",
                bill.get("department") or "-",
                "Rate Category",
                bill.get("rate_category") or "-",
                "Bed No.",
                bill.get("bed_number") or "-",
                styles,
            ),
            _meta_row(
                "Doctor",
                bill.get("doctor_name") or "-",
                "Adm. Date",
                _date_text(bill.get("admission_date"), True),
                "Discharge Date",
                _date_text(bill.get("discharge_date"), True),
                styles,
            ),
            _meta_row(
                "Billed By",
                bill.get("created_by_name") or "-",
                "",
                "",
                "Room Type",
                bill.get("room_type_name") or bill.get("current_room_type_name") or bill.get("room_type") or bill.get("current_room_type") or "-",
                styles,
            ),
           
            Spacer(1, 2 * mm),
            _charge_table(_aggregate_ip_auto_items(_ip_display_items(bill, items)), styles, False),
            Spacer(1, 2 * mm),
            *(
                [
                    _p("PAYMENT HISTORY", styles["bold"]),
                    Spacer(1, 1 * mm),
                    _transaction_table(payments, styles, False, False),
                    Spacer(1, 2 * mm),
                ]
                if payments else []
            ),
            *(
                [
                    _p("REFUND HISTORY", styles["bold"]),
                    Spacer(1, 1 * mm),
                    _transaction_table(refunds, styles, False, True),
                    Spacer(1, 3 * mm),
                ]
                if refunds else []
            ),
        ])

        gst_total = sum(
            _money(item.get("gst_amount"))
            for item in items
        )

        
        summary = Table([
            [_p("Bill Amount", styles["normal"]), _p(f"{subtotal:,.2f}", styles["normal"])],
            [_p("Discount :", styles["normal"]), _p(f"{discount_total:,.2f}", styles["normal"])],
            [_p("GST :", styles["normal"]), _p(f"{gst_total:,.2f}", styles["normal"])],
            [_p("Net Amount :", styles["normal"]), _p(f"{net_total:,.2f}", styles["normal"])],
            [_p("Total Payments :", styles["normal"]), _p(f"{total_payments:,.2f}", styles["normal"])],
            [_p("Total Refunds :", styles["normal"]), _p(f"{total_refunds:,.2f}", styles["normal"])],
            [_p("Net Paid Amount :", styles["bold"]), _p(f"{net_paid:,.2f}", styles["bold"])],
            *([[ _p("Deposit Balance :", styles["bold"]), _p(f"{max(0.0, net_paid - net_total):,.2f}", styles["bold"]) ]] if not is_op else []),
            [_p("Balance :", styles["bold"]), _p(f"{balance:,.2f}", styles["bold"])],
            [
                _p("Remarks:", styles["normal"]),
                _p(
                    bill.get("notes")
                    or "(All figures are in Rupees (INR) only)",
                    styles["normal"],
                ),
            ],
        ], colWidths=[42 * mm, CONTENT_WIDTH - 42 * mm])


        summary.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, -2), 0.35, GRID),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))

        story.extend([
            summary,
            Spacer(1, 5 * mm),
            Table([
                [
                    _p(_display(bill.get("printed_by") or "System"), styles["normal"]),
                    _p(_display(bill.get("doctor_name") or "Primary Doctor"), styles["normal"]),
                ],
                [
                    _p("PRINTED BY", styles["footer"]),
                    _p("PRIMARY DOCTOR", styles["footer"]),
                ],
                [
                    _p(_date_text(datetime.now(ZoneInfo("Asia/Kolkata")), True), styles["footer"]),
                    "",
                ],
            ], colWidths=[CONTENT_WIDTH / 2, CONTENT_WIDTH / 2]),
        ])

        filename = _download_filename(bill, "IP", bill_id)

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=(30 if is_op else 33) * mm,
        bottomMargin=20 * mm,
        title=filename[:-4],
        author="Maarga Mindcare",
    )

    try:
        def page_callback(canvas, document):
            _header_footer(canvas, document, expected_type)

        doc.build(
            story,
            onFirstPage=page_callback,
            onLaterPages=page_callback,
        )
    except Exception as exc:
        try:
            from Core.logger import get_logger

            get_logger(__name__).exception(
                "Unable to generate %s bill PDF for bill %s",
                expected_type,
                bill_id,
            )
        except Exception:
            pass

        raise HTTPException(
            status_code=500,
            detail=f"Unable to generate the {expected_type} bill PDF.",
        ) from exc

    return Response(
        content=buffer.getvalue(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def generate_ip_bill_pdf(bill_id: int) -> Response:
    return _generate_bill_pdf(bill_id, "IP")


def generate_op_bill_pdf(bill_id: int) -> Response:
    return _generate_bill_pdf(bill_id, "OP")
