
from __future__ import annotations
from io import BytesIO
import logging
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
from xml.sax.saxutils import escape

from fastapi import HTTPException
from fastapi.responses import Response
from psycopg2.extras import RealDictCursor

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm

from reportlab.platypus import (
    Image,
    KeepInFrame,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from reportlab.graphics.barcode import code128

from Utils.billing_helpers import get_db_connection


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent.parent
ASSETS_DIR = BASE_DIR / "assets"

HOSPITAL_NAME = "MAARGA MINDCARE PRIVATE LIMITED"

HOSPITAL_ADDRESS = (
    "23/B, Sector A, Yelahanka New Town, "
    "Bengaluru, Karnataka-560064"
)

HOSPITAL_WEBSITE = "www.maargamindcare.com"
HOSPITAL_EMAIL = "maarga@maargamindcare.com"
HOSPITAL_PHONE = "080-46659999"

TIMEZONE = ZoneInfo("Asia/Kolkata")

BLACK = colors.HexColor("#000000")
GREY = colors.HexColor("#777777")

PAGE_WIDTH, PAGE_HEIGHT = A4

LEFT_MARGIN = 18 * mm
RIGHT_MARGIN = 18 * mm
TOP_MARGIN = 12 * mm
BOTTOM_MARGIN = 12 * mm

CONTENT_WIDTH = PAGE_WIDTH - LEFT_MARGIN - RIGHT_MARGIN


# ============================================================
# GENERAL HELPERS
# ============================================================

def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value or 0)).quantize(
            Decimal("0.01")
        )
    except (TypeError, ValueError, ArithmeticError):
        return Decimal("0.00")


def _date_value(value: Any):
    if not value:
        return None

    if isinstance(value, (datetime, date)):
        return value

    try:
        return datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None


def _date_text(value: Any, include_time=False) -> str:
    parsed = _date_value(value)

    if not parsed:
        return str(value) if value else "-"

    if isinstance(parsed, datetime):
        if parsed.tzinfo:
            parsed = parsed.astimezone(TIMEZONE)

        if include_time:
            return parsed.strftime("%Y-%m-%d %H:%M:%S")

        return parsed.strftime("%Y-%m-%d")

    if include_time:
        return datetime.combine(
            parsed,
            datetime.min.time(),
        ).strftime("%Y-%m-%d %H:%M:%S")

    return parsed.strftime("%Y-%m-%d")


def _display(value: Any) -> str:
    if value is None:
        return "-"

    value = str(value).strip()
    return value if value else "-"


def _p(value: Any, style: ParagraphStyle) -> Paragraph:
    text = escape(_display(value))
    text = text.replace("\n", "<br/>")
    return Paragraph(text, style)


def _first_value(source: dict, *keys):
    if not isinstance(source, dict):
        return None

    for key in keys:
        value = source.get(key)

        if value is not None and str(value).strip():
            return value

    return None


# ============================================================
# PATIENT DETAILS
# ============================================================

def _calculate_age(dob: Any):

    parsed = _date_value(dob)

    if not parsed:
        return None

    birth_date = (
        parsed.date()
        if isinstance(parsed, datetime)
        else parsed
    )

    today = datetime.now(TIMEZONE).date()

    age = today.year - birth_date.year

    if (today.month, today.day) < (
        birth_date.month,
        birth_date.day,
    ):
        age -= 1

    return age


def _patient_details(payment: dict) -> dict:

    patient_data = payment.get("patient_data") or {}
    bill_data = payment.get("bill_data") or {}

    if not isinstance(patient_data, dict):
        patient_data = {}

    if not isinstance(bill_data, dict):
        bill_data = {}

    # AGE
    age = _calculate_age(
        patient_data.get("date_of_birth")
    )

    # GENDER
    gender = _first_value(
        patient_data,
        "gender",
    )

    if age is not None and gender:
        age_gender = f"{age} Y, {gender}"
    elif age is not None:
        age_gender = f"{age} Y"
    elif gender:
        age_gender = str(gender)
    else:
        age_gender = "-"

    # ADDRESS
    address = _first_value(
        patient_data,
        "address",
    )

    pincode = _first_value(
        patient_data,
        "pincode",
    )

    if address and pincode:
        address = f"{address}, {pincode}"

    # CONSULTANT
    consultant = (
        _first_value(
            bill_data,
            "doctor_name",
            "psychologist_name",
        )
        or "-"
    )

    # SPONSOR
    sponsor = payment.get("patient_sponsor") or "-"

    # REGISTRATION DATE
    registration_date = _date_text(
        patient_data.get("registered_at")
    )

    return {
        "age_gender": age_gender,
        "address": address or "-",
        "consultant": consultant,
        "sponsor": sponsor,
        "registration_date": registration_date,
    }


# ============================================================
# AMOUNT IN WORDS
# ============================================================

def _number_to_words(number: int) -> str:

    ones = [
        "", "One", "Two", "Three", "Four",
        "Five", "Six", "Seven", "Eight", "Nine",
        "Ten", "Eleven", "Twelve", "Thirteen",
        "Fourteen", "Fifteen", "Sixteen",
        "Seventeen", "Eighteen", "Nineteen",
    ]

    tens = [
        "", "", "Twenty", "Thirty", "Forty",
        "Fifty", "Sixty", "Seventy", "Eighty",
        "Ninety",
    ]

    def convert(value: int) -> str:

        if value == 0:
            return ""

        if value < 20:
            return ones[value]

        if value < 100:
            return tens[value // 10] + (
                " " + ones[value % 10]
                if value % 10 else ""
            )

        if value < 1000:
            return (
                ones[value // 100]
                + " Hundred"
                + (
                    " " + convert(value % 100)
                    if value % 100 else ""
                )
            )

        return ""

    if number == 0:
        return "Zero"

    parts = []

    crore = number // 10000000
    number %= 10000000

    lakh = number // 100000
    number %= 100000

    thousand = number // 1000
    number %= 1000

    if crore:
        parts.append(convert(crore) + " Crore")

    if lakh:
        parts.append(convert(lakh) + " Lakh")

    if thousand:
        parts.append(convert(thousand) + " Thousand")

    if number:
        parts.append(convert(number))

    return " ".join(parts)


def _amount_in_words(amount: Decimal) -> str:

    amount = _money(amount)

    rupees = int(amount)

    paise = int(
        (amount - Decimal(rupees)) * 100
    )

    result = _number_to_words(rupees)

    if paise:
        result += (
            " and "
            + _number_to_words(paise)
            + " Paise"
        )

    return result + " Only"


# ============================================================
# PDF STYLES
# ============================================================

def _styles():

    sample = getSampleStyleSheet()

    return {

        "hospital": ParagraphStyle(
            "Hospital",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=13,
            textColor=BLACK,
            alignment=TA_LEFT,
        ),

        "address": ParagraphStyle(
            "HospitalAddress",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=BLACK,
            alignment=TA_LEFT,
        ),

        "receipt_title": ParagraphStyle(
            "ReceiptTitle",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=13,
            textColor=BLACK,
            alignment=TA_CENTER,
        ),

        "normal": ParagraphStyle(
            "ReceiptNormal",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=BLACK,
            alignment=TA_LEFT,
            wordWrap="CJK",
        ),

        "right": ParagraphStyle(
            "ReceiptRight",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=BLACK,
            alignment=TA_RIGHT,
        ),

        "table_header": ParagraphStyle(
            "ReceiptTableHeader",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=BLACK,
            alignment=TA_LEFT,
        ),

        "table_normal": ParagraphStyle(
            "ReceiptTableNormal",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=BLACK,
            alignment=TA_LEFT,
            wordWrap="CJK",
        ),

        "table_right": ParagraphStyle(
            "ReceiptTableRight",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=10,
            textColor=BLACK,
            alignment=TA_RIGHT,
        ),

        "table_bold_right": ParagraphStyle(
            "ReceiptTableBoldRight",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=BLACK,
            alignment=TA_RIGHT,
        ),

        "amount_words": ParagraphStyle(
            "ReceiptAmountWords",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=11,
            textColor=BLACK,
            alignment=TA_LEFT,
        ),

        "detail_label": ParagraphStyle(
            "PaymentDetailLabel",
            parent=sample["Normal"],
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=11,
            textColor=BLACK,
            alignment=TA_LEFT,
        ),

        "detail_value": ParagraphStyle(
            "PaymentDetailValue",
            parent=sample["Normal"],
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=BLACK,
            alignment=TA_LEFT,
            wordWrap="CJK",
        ),
    }


# ============================================================
# BARCODE
# ============================================================

def _make_barcode(
    value: Any,
    max_width: float,
    max_height: float = 8 * mm,
):

    if not value:
        return ""

    barcode = code128.Code128(
        str(value),
        barHeight=max_height,
        barWidth=0.32,
        humanReadable=False,
        quiet=True,
    )

    return KeepInFrame(
        maxWidth=max_width,
        maxHeight=max_height + 1 * mm,
        content=[barcode],
        mode="shrink",
        hAlign="CENTER",
        vAlign="MIDDLE",
    )


# ============================================================
# HOSPITAL HEADER
# ============================================================

def _make_header(styles):

    logo_path = ASSETS_DIR / "maarga_logo.png"

    if logo_path.exists():

        logo = Image(
            str(logo_path),
            width=20 * mm,
            height=18 * mm,
            kind="proportional",
        )

        logo.hAlign = "RIGHT"

    else:
        logo = Spacer(20 * mm, 18 * mm)

    hospital_details = [

        _p(
            HOSPITAL_NAME,
            styles["hospital"],
        ),

        Spacer(1, 3 * mm),

        _p(
            HOSPITAL_ADDRESS,
            styles["address"],
        ),
    ]

    header = Table(
        [[hospital_details, logo]],

        colWidths=[
            CONTENT_WIDTH * 0.80,
            CONTENT_WIDTH * 0.20,
        ],
    )

    header.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),

                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),

                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    return header


# ============================================================
# UHID / RECEIPT / OP-IP ROW
# ============================================================

def _make_identifier_row(
    patient_id,
    type_number,
    styles,
):

    # Total width = CONTENT_WIDTH.
    # Both barcode cells stay on the same horizontal row.

    widths = [
        26 * mm,  # UHID text
        32 * mm,  # UHID barcode
        45 * mm,  # RECEIPT title
        38 * mm,  # OP/IP number
        33 * mm,  # OP/IP barcode
    ]

    uhid_barcode = _make_barcode(
        patient_id,
        max_width=widths[1] - 3 * mm,
    )

    op_ip_barcode = _make_barcode(
        type_number,
        max_width=widths[4] - 3 * mm,
    )

    identifier_row = Table(
        [
            [
                _p(
                    f"UHID : {patient_id or '-'}",
                    styles["normal"],
                ),

                uhid_barcode,

                _p(
                    "RECEIPT",
                    styles["receipt_title"],
                ),

                _p(
                    f"OP/IP No : {type_number or '-'}",
                    styles["right"],
                ),

                op_ip_barcode,
            ]
        ],

        colWidths=widths,
    )

    identifier_row.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),

                ("LINEABOVE", (0, 0), (-1, 0), 0.6, BLACK),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, BLACK),

                ("ALIGN", (1, 0), (1, 0), "CENTER"),
                ("ALIGN", (2, 0), (2, 0), "CENTER"),
                ("ALIGN", (4, 0), (4, 0), "RIGHT"),

                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),

                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    return identifier_row


# ============================================================
# PATIENT AND BILL DETAILS
# ============================================================

def _make_patient_table(payment, styles):

    details = _patient_details(payment)

    patient_rows = [

        [
            _p(
                f"Patient       : {payment.get('patient_name')}",
                styles["normal"],
            ),

            _p(
                f"Bill No       : {payment.get('bill_number')}",
                styles["normal"],
            ),
        ],

        [
            _p(
                f"Age / Gender  : {details['age_gender']}",
                styles["normal"],
            ),

            _p(
                f"Bill Date     : {_date_text(payment.get('bill_date'), include_time=True)}",
                styles["normal"],
            ),
        ],

        [
            _p(
                f"Address       : {details['address']}",
                styles["normal"],
            ),

            _p(
                f"Reg. Date     : {details['registration_date']}",
                styles["normal"],
            ),
        ],

        [
            _p(
                f"Consultant    : {details['consultant']}",
                styles["normal"],
            ),

            _p(
                f"Phone         : {payment.get('mobile')}",
                styles["normal"],
            ),
        ],

        [
            _p(
                f"Sponsor       : {details['sponsor']}",
                styles["normal"],
            ),

            _p(
                f"Bill Type     : {payment.get('bill_type')}",
                styles["normal"],
            ),
        ],
    ]

    patient_table = Table(
        patient_rows,

        colWidths=[
            CONTENT_WIDTH * 0.70,
            CONTENT_WIDTH * 0.30,
        ],
    )

    patient_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),

                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),

                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    return patient_table


# ============================================================
# PAYMENT TABLE
# ============================================================

def _make_receipt_table(payment, styles):

    amount = _money(payment.get("amount"))

    receipt_number = (
        payment.get("receipt_number")
        or f"RCT-{payment.get('id')}"
    )

    payment_date = _date_text(
        payment.get("payment_date"),
        include_time=True,
    )

    rows = [

        [
            _p("Rec. No", styles["table_header"]),
            _p("Date", styles["table_header"]),
            _p("Status", styles["table_header"]),
            _p("Mode of Payment", styles["table_header"]),
            _p("Amount", styles["table_header"]),
        ],

        [
            _p(receipt_number, styles["table_normal"]),
            _p(payment_date, styles["table_normal"]),
            _p("Received", styles["table_normal"]),
            _p(payment.get("payment_method"), styles["table_normal"]),
            _p(f"{amount:,.2f}", styles["table_right"]),
        ],

        [
            "",
            "",
            "",
            _p("Total", styles["table_bold_right"]),
            _p(f"{amount:,.2f}", styles["table_bold_right"]),
        ],
    ]

    table = Table(
        rows,

        colWidths=[
            CONTENT_WIDTH * 0.19,
            CONTENT_WIDTH * 0.30,
            CONTENT_WIDTH * 0.10,
            CONTENT_WIDTH * 0.15,
            CONTENT_WIDTH * 0.26,
        ],

        rowHeights=[
            7 * mm,
            7 * mm,
            7 * mm,
        ],
    )

    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, 1), 0.6, BLACK),

                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),

                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),

                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),

                ("ALIGN", (4, 1), (4, 2), "RIGHT"),
                ("ALIGN", (3, 2), (3, 2), "RIGHT"),

                ("SPAN", (0, 2), (2, 2)),
            ]
        )
    )

    return table


# ============================================================
# TRANSACTION REFERENCE NUMBER AND NOTES
# ============================================================

def _make_payment_extra_details(payment, styles):

    reference_number = payment.get("reference_number")
    notes = payment.get("notes")

    rows = []

    if reference_number:

        rows.append(
            [
                _p(
                    "Transaction / Reference Number :",
                    styles["detail_label"],
                ),

                _p(
                    reference_number,
                    styles["detail_value"],
                ),
            ]
        )

    if notes:

        rows.append(
            [
                _p(
                    "Notes :",
                    styles["detail_label"],
                ),

                _p(
                    notes,
                    styles["detail_value"],
                ),
            ]
        )

    if not rows:
        return None

    details_table = Table(
        rows,

        colWidths=[
            CONTENT_WIDTH * 0.30,
            CONTENT_WIDTH * 0.70,
        ],
    )

    details_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),

                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),

                ("TOPPADDING", (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]
        )
    )

    return details_table


# ============================================================
# FOOTER AS A NORMAL FLOWABLE
# ============================================================

def _make_footer(
    styles,
    printed_by="System",
    prepared_by="System",
):

    print_date = datetime.now(
        TIMEZONE
    ).strftime("%Y-%m-%d %H:%M")

    footer_rows = [

        [
            _p(
                f"Printed By : {printed_by}",
                styles["normal"],
            ),

            _p(
                f"Prepared By : {prepared_by}",
                styles["normal"],
            ),

            _p(
                f"Print Date : {print_date}",
                styles["normal"],
            ),

            _p(
                "Authorized Signature",
                styles["right"],
            ),
        ],

        [
            _p(
                f"url: {HOSPITAL_WEBSITE} || "
                f"email: {HOSPITAL_EMAIL} || "
                f"phone: {HOSPITAL_PHONE}",
                styles["normal"],
            ),

            "",
            "",
            "",
        ],
    ]

    footer = Table(
        footer_rows,

        colWidths=[
            CONTENT_WIDTH * 0.23,
            CONTENT_WIDTH * 0.23,
            CONTENT_WIDTH * 0.29,
            CONTENT_WIDTH * 0.25,
        ],
    )

    footer.setStyle(
        TableStyle(
            [
                # Line above the footer
                ("LINEABOVE", (0, 0), (-1, 0), 0.6, GREY),

                # Line below the first footer row
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, GREY),

                # Contact details span the full width
                ("SPAN", (0, 1), (-1, 1)),

                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),

                ("LEFTPADDING", (0, 0), (-1, -1), 2),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),

                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )

    return footer


# ============================================================
# MAIN PAYMENT RECEIPT PDF GENERATOR
# ============================================================

def generate_payment_receipt_pdf(
    payment_id: int,
) -> Response:

    connection = None

    try:

        # ----------------------------------------------------
        # DATABASE QUERY
        # ----------------------------------------------------

        connection = get_db_connection()

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            cursor.execute(
                """
                SELECT

                    -- PAYMENT DETAILS
                    bp.id,
                    bp.receipt_number,
                    bp.bill_id,
                    bp.amount,
                    bp.payment_date,
                    bp.payment_method,
                    bp.reference_number,
                    bp.notes,
                    bp.created_by,

                    -- BILL DETAILS
                    b.bill_number,
                    b.bill_type,
                    b.op_number,
                    b.ip_number,
                    b.bill_date,
                    b.total_amount AS bill_total,

                    b.doctor_name,
                    b.psychologist_name,

                    -- PATIENT DETAILS
                    p.name AS patient_name,
                    p.patient_id,
                    p.mobile,

                    -- SPONSOR DETAILS
                    COALESCE(
                        NULLIF(
                            BTRIM(b.sponsor_name_snapshot),
                            ''
                        ),
                        s.sponsor_name
                    ) AS patient_sponsor,

                    -- FULL JSON RECORDS
                    to_jsonb(p) AS patient_data,
                    to_jsonb(b) AS bill_data,

                    -- PAYMENT CREATOR
                    u.full_name AS collected_by

                FROM public.bill_payments bp

                INNER JOIN public.bills b
                    ON b.id = bp.bill_id

                INNER JOIN public.patients p
                    ON p.id = b.patient_db_id

                LEFT JOIN public.sponsors s
                    ON s.id = b.sponsor_id

                LEFT JOIN public.users u
                    ON u.id = bp.created_by

                WHERE bp.id = %s

                LIMIT 1
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

        # ----------------------------------------------------
        # BASIC VALUES
        # ----------------------------------------------------

        bill_type = str(
            payment.get("bill_type") or "OP"
        ).strip().upper()

        bill_type = (
            "IP"
            if bill_type == "IP"
            else "OP"
        )

        patient_id = payment.get("patient_id")

        type_number = (
            payment.get("ip_number")
            if bill_type == "IP"
            else payment.get("op_number")
        )

        receipt_number = (
            payment.get("receipt_number")
            or f"RCT-{payment.get('id')}"
        )

        amount = _money(
            payment.get("amount")
        )

        # ----------------------------------------------------
        # STYLES
        # ----------------------------------------------------

        styles = _styles()

        # ----------------------------------------------------
        # HEADER
        # ----------------------------------------------------

        header = _make_header(styles)

        # ----------------------------------------------------
        # UHID / RECEIPT / OP-IP ROW
        # ----------------------------------------------------

        identifier_row = _make_identifier_row(
            patient_id=patient_id,
            type_number=type_number,
            styles=styles,
        )

        # ----------------------------------------------------
        # PATIENT DETAILS
        # ----------------------------------------------------

        patient_table = _make_patient_table(
            payment=payment,
            styles=styles,
        )

        # ----------------------------------------------------
        # PAYMENT TABLE
        # ----------------------------------------------------

        payment_table = _make_receipt_table(
            payment=payment,
            styles=styles,
        )

        # ----------------------------------------------------
        # REFERENCE NUMBER AND NOTES
        # ----------------------------------------------------

        payment_extra_details = _make_payment_extra_details(
            payment=payment,
            styles=styles,
        )

        # ----------------------------------------------------
        # AMOUNT IN WORDS
        # ----------------------------------------------------

        amount_words = _p(
            (
                "Received sum of rupees "
                + _amount_in_words(amount)
            ),
            styles["amount_words"],
        )

        # ----------------------------------------------------
        # FOOTER
        # ----------------------------------------------------

        footer = _make_footer(
            styles=styles,
            printed_by="System",
            prepared_by=(
                payment.get("collected_by")
                or "System"
            ),
        )

        # ----------------------------------------------------
        # COMPLETE RECEIPT STORY
        # ----------------------------------------------------

        story = [

            header,

            Spacer(1, 2 * mm),

            identifier_row,

            Spacer(1, 2 * mm),

            patient_table,

            Spacer(1, 9 * mm),

            payment_table,

            Spacer(1, 3 * mm),
        ]

        # Add reference number and notes only when present.
        if payment_extra_details:

            story.extend(
                [
                    payment_extra_details,
                    Spacer(1, 5 * mm),
                ]
            )

        else:

            story.append(
                Spacer(1, 2 * mm)
            )

        story.extend(
            [
                amount_words,

                # Space before footer
                Spacer(1, 12 * mm),

                # Footer follows the receipt content
                footer,
            ]
        )

        # ----------------------------------------------------
        # BUILD STANDARD A4 PDF
        # ----------------------------------------------------

        buffer = BytesIO()

        filename = f"Receipt-{receipt_number}.pdf"

        document = SimpleDocTemplate(
            buffer,

            pagesize=A4,

            leftMargin=LEFT_MARGIN,
            rightMargin=RIGHT_MARGIN,

            topMargin=TOP_MARGIN,
            bottomMargin=BOTTOM_MARGIN,

            title=f"Payment Receipt - {receipt_number}",
            author=HOSPITAL_NAME,

            pageCompression=1,
        )

        document.build(story)

        pdf_content = buffer.getvalue()
        buffer.close()

        # ----------------------------------------------------
        # RETURN PDF
        # ----------------------------------------------------

        return Response(
            content=pdf_content,

            media_type="application/pdf",

            headers={
                "Content-Disposition": (
                    f'inline; filename="{filename}"'
                ),

                "Cache-Control": "no-store",
            },
        )

    except HTTPException:
        raise

    except Exception as exc:

        logging.getLogger(__name__).exception(
            "Unable to generate payment receipt PDF "
            "for payment ID %s",
            payment_id,
        )

        raise HTTPException(
            status_code=500,
            detail="Unable to generate the payment receipt PDF.",
        ) from exc

    finally:

        if connection is not None:
            connection.close()
