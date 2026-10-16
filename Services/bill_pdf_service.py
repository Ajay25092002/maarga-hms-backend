
from __future__ import annotations

import os

from datetime import date, datetime
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

        today = date.today()

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
    # collections and outstanding balance, not the original bill value.
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
    balance = max(0.0, net_total - net_paid)

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
            return psycopg2.connect(value.strip(), connect_timeout=10)
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
                    pr.payment_id,
                    pr.refund_amount,
                    pr.refund_method,
                    pr.reason,
                    pr.notes,
                    pr.created_by,
                    pr.created_at,
                    bp.receipt_number,
                    bp.payment_method AS original_payment_method
                FROM public.payment_refunds pr
                JOIN public.bill_payments bp
                    ON bp.id = pr.payment_id
                WHERE bp.bill_id = %s
                ORDER BY pr.created_at ASC, pr.id ASC
                """,
                (bill_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
    finally:
        connection.close()


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
        grouped.setdefault(
            str(item.get("category") or "OTHER CHARGES").strip(),
            [],
        ).append(item)

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
    row = []

    for label, value in [
        (label1, value1),
        (label2, value2),
        (label3, value3),
    ]:
        if label and value not in ("", None, "-"):
            row.extend([
                _p(label, styles["bold"]),
                _p(value, styles["normal"]),
            ])

    col_widths = [
        20 * mm,
        35 * mm,
        20 * mm,
        35 * mm,
        20 * mm,
        CONTENT_WIDTH - 130 * mm,
    ]

    table = Table([row], colWidths=col_widths, hAlign="LEFT")
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
            _p("PAYMENT HISTORY", styles["bold"]),
            Spacer(1, 1 * mm),
            _transaction_table(payments, styles, True, False),
            Spacer(1, 2 * mm),
            _p("REFUND HISTORY", styles["bold"]),
            Spacer(1, 1 * mm),
            _transaction_table(refunds, styles, True, True),
            Spacer(1, 4 * mm),
            _p(
                f"Printed By : {_display(bill.get('printed_by') or 'System')} "
                f"Prepared By : {_display(bill.get('prepared_by') or 'System')} "
                f"Print Date : {_date_text(datetime.now(), True)}",
                styles["footer"],
            ),
            Spacer(1, 1 * mm),
            _p(
                "Authorized Signature: _______________________________________________",
                styles["right"],
            ),
        ])

        filename = f"OP-Bill-{bill.get('bill_number') or bill_id}.pdf"

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
           
            Spacer(1, 2 * mm),
            _charge_table(items, styles, False),
            Spacer(1, 2 * mm),
            _p("PAYMENT AND REFUND HISTORY", styles["bold"]),
            Spacer(1, 1 * mm),
            _p("PAYMENT HISTORY", styles["bold"]),
            Spacer(1, 1 * mm),
            _transaction_table(payments, styles, False, False),
            Spacer(1, 2 * mm),
            _p("REFUND HISTORY", styles["bold"]),
            Spacer(1, 1 * mm),
            _transaction_table(refunds, styles, False, True),
            Spacer(1, 3 * mm),
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
                    _p(_date_text(datetime.now(), True), styles["footer"]),
                    "",
                ],
            ], colWidths=[CONTENT_WIDTH / 2, CONTENT_WIDTH / 2]),
        ])

        filename = f"IP-Bill-{bill.get('bill_number') or bill_id}.pdf"

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
