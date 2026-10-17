from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from fastapi import HTTPException
from fastapi.responses import Response
from psycopg2.extras import RealDictCursor
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from Utils.billing_helpers import get_db_connection
from Services.bill_query_service import get_bill_service


BASE_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = BASE_DIR / "assets"

BLACK = colors.HexColor("#111111")
GREY = colors.HexColor("#D0D0D0")
GRID = colors.HexColor("#999999")
LIGHT_GREY = colors.HexColor("#F2F2F2")
FOOTER = colors.HexColor("#555555")
CONTENT_WIDTH = A4[0] - 30 * mm


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0.00")


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

    fmt = "%d-%m-%Y %H:%M" if include_time else "%d-%m-%Y"
    return parsed.strftime(fmt)


def _display(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _p(value: Any, style: ParagraphStyle) -> Paragraph:
    # Escape database values so characters such as &, < and > do not
    # break ReportLab's paragraph parser.
    text = escape(_display(value)).replace("\n", "<br/>")
    return Paragraph(text, style)


def _styles():
    sample = getSampleStyleSheet()

    return {
        "hospital": ParagraphStyle(
            "ReceiptHospital",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=14,
            textColor=BLACK,
            alignment=TA_LEFT,
        ),
        "address": ParagraphStyle(
            "ReceiptAddress",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9,
            textColor=BLACK,
            alignment=TA_LEFT,
        ),
        "title": ParagraphStyle(
            "ReceiptTitle",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=10,
            leading=12,
            textColor=BLACK,
            alignment=TA_CENTER,
        ),
        "normal": ParagraphStyle(
            "ReceiptNormal",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9,
            textColor=BLACK,
        ),
        "small": ParagraphStyle(
            "ReceiptSmall",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=6.8,
            leading=8,
            textColor=BLACK,
        ),
        "bold": ParagraphStyle(
            "ReceiptBold",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7.5,
            leading=9,
            textColor=BLACK,
        ),
        "right": ParagraphStyle(
            "ReceiptRight",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=7.3,
            leading=9,
            textColor=BLACK,
            alignment=TA_RIGHT,
        ),
        "center": ParagraphStyle(
            "ReceiptCenter",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=7,
            leading=8,
            textColor=BLACK,
            alignment=TA_CENTER,
        ),
        "table_head": ParagraphStyle(
            "ReceiptTableHead",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7,
            leading=8,
            textColor=BLACK,
        ),
        "footer": ParagraphStyle(
            "ReceiptFooter",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=6.5,
            leading=8,
            textColor=FOOTER,
        ),
    }


def _header_footer(canvas, doc, bill_type: str):
    """Use the same OP/IP hospital branding as the bill generator."""
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
            "Website: www.maargamindcare.com | Email: maarga@maargamindcare.com",
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
            "A-14, Golfcourse Ext road, Block A, Sushant Lok III, "
            "Sector 57, Gurugram, Haryana, 122011",
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
    return TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.45, GRID),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]
    )


def _receipt_items_table(items: list[dict], styles, bill_type: str):
    """Build an itemized service table using the same item data as the bill PDF."""
    if bill_type == "IP":
        widths = [
            11 * mm,
            21 * mm,
            CONTENT_WIDTH - 11 * mm - 21 * mm - 46 * mm - 12 * mm - 19 * mm - 19 * mm - 20 * mm,
            46 * mm,
            12 * mm,
            19 * mm,
            19 * mm,
            20 * mm,
        ]
        headings = [
            "Sl No",
            "Date",
            "Service Name",
            "Rate",
            "Qty",
            "Amount",
            "Discount",
            "Net Amount",
        ]
    else:
        widths = [
            12 * mm,
            CONTENT_WIDTH - 12 * mm - 26 * mm - 13 * mm - 22 * mm - 22 * mm - 25 * mm,
            26 * mm,
            13 * mm,
            22 * mm,
            22 * mm,
            25 * mm,
        ]
        headings = [
            "Sl No",
            "Service Name",
            "Rate",
            "Qty",
            "Amount",
            "Discount",
            "Net Amount",
        ]

    rows = [[_p(heading, styles["table_head"]) for heading in headings]]

    for index, item in enumerate(items, start=1):
        quantity = _money(item.get("quantity") or 1)
        amount = _money(
            item.get(
                "amount",
                _money(item.get("rate")) * quantity,
            )
        )
        discount = _money(item.get("discount_amount"))
        net_amount = _money(item.get("net_amount", amount - discount))
        service_name = (
            item.get("service_name")
            or item.get("name")
            or item.get("description")
            or item.get("service_code")
            or "-"
        )

        if bill_type == "IP":
            rows.append([
                _p(index, styles["small"]),
                _p(_date_text(item.get("service_date") or item.get("created_at")), styles["small"]),
                _p(service_name, styles["small"]),
                _p(f"{_money(item.get('rate')):,.2f}", styles["right"]),
                _p(item.get("quantity") or 1, styles["right"]),
                _p(f"{amount:,.2f}", styles["right"]),
                _p(f"{discount:,.2f}", styles["right"]),
                _p(f"{net_amount:,.2f}", styles["right"]),
            ])
        else:
            rows.append([
                _p(index, styles["small"]),
                _p(service_name, styles["small"]),
                _p(f"{_money(item.get('rate')):,.2f}", styles["right"]),
                _p(item.get("quantity") or 1, styles["right"]),
                _p(f"{amount:,.2f}", styles["right"]),
                _p(f"{discount:,.2f}", styles["right"]),
                _p(f"{net_amount:,.2f}", styles["right"]),
            ])

    if len(rows) == 1:
        empty_row = [_p("No billed service items found.", styles["normal"])]
        empty_row.extend([""] * (len(headings) - 1))
        rows.append(empty_row)

    table = Table(rows, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(_table_style())
    table.setStyle(
        TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), GREY),
            ("ALIGN", (3 if bill_type == "IP" else 2, 1), (-1, -1), "RIGHT"),
        ])
    )
    return table


def generate_payment_receipt_pdf(payment_id: int) -> Response:
    """Generate a PDF receipt for an existing bill payment."""
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    bp.id,
                    bp.receipt_number,
                    bp.bill_id,
                    bp.amount,
                    bp.payment_date,
                    bp.payment_method,
                    bp.reference_number,
                    bp.notes,

                    b.bill_number,
                    b.bill_type,
                    b.op_number,
                    b.ip_number,
                    b.total_amount AS bill_total,
                    b.bill_date,

                    p.name AS patient_name,
                    p.patient_id,
                    p.mobile,

                    COALESCE(
                        (
                            SELECT SUM(p2.amount)
                            FROM public.bill_payments p2
                            WHERE p2.bill_id = b.id
                        ),
                        0
                    ) AS bill_amount_paid,
                    COALESCE(
                        (
                            SELECT SUM(pr.refund_amount)
                            FROM public.payment_refunds pr
                            WHERE pr.bill_id = b.id
                        ),
                        0
                    ) AS bill_total_refunded

                FROM public.bill_payments bp
                JOIN public.bills b
                    ON b.id = bp.bill_id
                JOIN public.patients p
                    ON p.id = b.patient_db_id
                WHERE bp.id = %s
                """,
                (payment_id,),
            )

            row = cursor.fetchone()

        if row is None:
            raise HTTPException(
                status_code=404,
                detail="Payment receipt not found.",
            )

        payment = dict(row)

        # Reuse the bill query service so the receipt uses the same individual
        # service items and item amounts as the corresponding OP/IP bill PDF.
        bill_data = get_bill_service(int(payment["bill_id"]))
        bill_items = bill_data.get("items") or []

        bill_type = str(payment.get("bill_type") or "OP").strip().upper()
        bill_type = "IP" if bill_type == "IP" else "OP"

        styles = _styles()

        receipt_number = (
            payment.get("receipt_number") or f"RCT-{payment['id']}"
        )
        bill_number = payment.get("bill_number") or f"#{payment['bill_id']}"

        type_number = (
            payment.get("ip_number")
            if bill_type == "IP"
            else payment.get("op_number")
        )

        bill_total = _money(payment.get("bill_total"))
        gross_paid = _money(payment.get("bill_amount_paid"))
        total_refunded = _money(payment.get("bill_total_refunded"))
        total_paid = max(gross_paid - total_refunded, Decimal("0.00"))
        outstanding = max(Decimal("0.00"), bill_total - gross_paid)
        amount = _money(payment.get("amount"))

        # Patient identifier is patient_id. Do not depend on the
        # removed registration_number database column.
        story = [
            Spacer(1, 1 * mm),
            Table(
                [
                    [
                        _p(
                            f"Patient ID: {payment.get('patient_id')}",
                            styles["bold"],
                        ),
                        _p("PAYMENT RECEIPT", styles["title"]),
                        _p(
                            f"Receipt No: {receipt_number}",
                            styles["right"],
                        ),
                    ]
                ],
                colWidths=[
                    CONTENT_WIDTH * 0.30,
                    CONTENT_WIDTH * 0.40,
                    CONTENT_WIDTH * 0.30,
                ],
            ),
            Spacer(1, 2 * mm),
        ]

        info_rows = [
            [
                _p(
                    f"Patient: {payment.get('patient_name')}",
                    styles["normal"],
                ),
                _p(f"Bill No: {bill_number}", styles["normal"]),
            ],
            [
                _p(f"Phone: {payment.get('mobile')}", styles["normal"]),
                _p(f"Bill Type: {bill_type}", styles["normal"]),
            ],
            [
                _p(
                    f"{'IP' if bill_type == 'IP' else 'OP'} Number: "
                    f"{type_number or '-'}",
                    styles["normal"],
                ),
                _p(
                    f"Bill Date: {_date_text(payment.get('bill_date'))}",
                    styles["normal"],
                ),
            ],
            [
                _p(
                    f"Payment Date: "
                    f"{_date_text(payment.get('payment_date'), True)}",
                    styles["normal"],
                ),
                _p(
                    f"Payment Mode: "
                    f"{payment.get('payment_method') or '-'}",
                    styles["normal"],
                ),
            ],
            [
                _p(
                    f"Reference No: "
                    f"{payment.get('reference_number') or '-'}",
                    styles["normal"],
                ),
                _p(
                    f"Notes: {payment.get('notes') or '-'}",
                    styles["normal"],
                ),
            ],
        ]

        info = Table(
            info_rows,
            colWidths=[CONTENT_WIDTH * 0.58, CONTENT_WIDTH * 0.42],
        )
        info.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 1),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )

        story.extend([info, Spacer(1, 3 * mm)])

        story.extend([
            _p("BILLED SERVICE ITEMS", styles["bold"]),
            Spacer(1, 1 * mm),
            _receipt_items_table(bill_items, styles, bill_type),
            Spacer(1, 3 * mm),
        ])

        details = [
            [
                _p("Description", styles["table_head"]),
                _p("Amount (Rs.)", styles["table_head"]),
            ],
            [
                _p("Amount received in this transaction", styles["normal"]),
                _p(f"{amount:,.2f}", styles["right"]),
            ],
            [
                _p("Total bill amount", styles["normal"]),
                _p(f"{bill_total:,.2f}", styles["right"]),
            ],
            [
                _p("Total payments recorded", styles["normal"]),
                _p(f"{gross_paid:,.2f}", styles["right"]),
            ],
            [
                _p("Total refunds", styles["normal"]),
                _p(f"{total_refunded:,.2f}", styles["right"]),
            ],
            [
                _p("Net paid against bill", styles["normal"]),
                _p(f"{total_paid:,.2f}", styles["right"]),
            ],
            [
                _p("Outstanding balance", styles["bold"]),
                _p(f"{outstanding:,.2f}", styles["right"]),
            ],
        ]

        detail_table = Table(
            details,
            colWidths=[CONTENT_WIDTH - 42 * mm, 42 * mm],
            hAlign="LEFT",
        )
        detail_table.setStyle(_table_style())
        detail_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), GREY),
                    ("BACKGROUND", (0, -1), (-1, -1), LIGHT_GREY),
                    ("ALIGN", (1, 1), (1, -1), "RIGHT"),
                ]
            )
        )

        story.extend([detail_table, Spacer(1, 5 * mm)])

        footer = Table(
            [
                [
                    _p(
                        f"Printed By: System    "
                        f"Print Date: {_date_text(datetime.now(), True)}",
                        styles["footer"],
                    ),
                    _p(
                        "Authorized Signature: __________________",
                        styles["right"],
                    ),
                ]
            ],
            colWidths=[CONTENT_WIDTH / 2, CONTENT_WIDTH / 2],
        )
        footer.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEABOVE", (0, 0), (-1, -1), 0.5, GRID),
                    ("TOPPADDING", (0, 0), (-1, -1), 8),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ]
            )
        )
        story.append(footer)

        buffer = BytesIO()
        filename = f"Receipt-{receipt_number}.pdf"

        document = SimpleDocTemplate(
            buffer,
            pagesize=A4,
            rightMargin=15 * mm,
            leftMargin=15 * mm,
            topMargin=(30 if bill_type == "OP" else 33) * mm,
            bottomMargin=20 * mm,
            title=filename[:-4],
            author="Maarga Mindcare",
        )

        def page_callback(canvas, doc):
            _header_footer(canvas, doc, bill_type)

        document.build(
            story,
            onFirstPage=page_callback,
            onLaterPages=page_callback,
        )

        return Response(
            content=buffer.getvalue(),
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    except HTTPException:
        raise

    except Exception as exc:
        from Core.logger import get_logger

        get_logger(__name__).exception(
            "Unable to generate payment receipt PDF for payment %s",
            payment_id,
        )

        raise HTTPException(
             status_code=500,
            detail="Unable to generate the payment receipt PDF.",
        ) from exc

    finally:
        if connection is not None:
            connection.close() 