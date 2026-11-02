#!/usr/bin/env python3
"""
Historical OP Bill Importer

Run:
    python import_historical_op_bills.py --plan-only
    python import_historical_op_bills.py --dry-run
    python import_historical_op_bills.py

Default Excel:
    OP_Bills_Manual_Entry_filled.xlsx

Rules:
- Exactly 63 OP bills are expected.
- Yellow-highlighted rows belong to the preceding bill.
- Excel rates and discounts are authoritative.
- Database-configured prices are never substituted.
- Bill and payment dates use the original Excel bill date.
- Existing Patient_ID values are reused.
- Missing Patient_ID values create a patient with name only.
- Existing System user is used as bill/payment creator and audit user.
- Bill Notes and Payment Notes remain empty.
- Audit history follows the backend bill_audit_service convention.
- Import tracking is stored in audit JSON, not visible bill notes.
- Each bill, its items, payment and audit records are transactional.
- Failed bills are reported without stopping other bills.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import sys
import uuid

from datetime import date, datetime, time
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

import openpyxl

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
except ImportError:
    pass


# ------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------

TZ = ZoneInfo("Asia/Kolkata")
CENT = Decimal("0.01")

EXPECTED_BILLS = 63

YELLOW = {
    "FFFFF2CC",
    "FFF2CC",
    "F2CC",
}

BACKEND_DIR = Path(__file__).resolve().parent

DEFAULT_EXCEL = (
    BACKEND_DIR / "OP_Bills_Manual_Entry_filled.xlsx"
)

ISSUE_FILE = "op_import_issues.csv"
REPORT_FILE = "op_import_report.csv"


# ------------------------------------------------------------
# APPLICATION LOGGER
# ------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            BACKEND_DIR / "historical_op_import.log",
            encoding="utf-8",
        ),
    ],
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# DATABASE IMPORTS
# ------------------------------------------------------------

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None


# ------------------------------------------------------------
# GENERAL HELPERS
# ------------------------------------------------------------

def blank(value):
    return value is None or (
        isinstance(value, str)
        and not value.strip()
    )


def money(value):
    if blank(value):
        value = 0

    return Decimal(str(value)).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )


def as_int(value):
    if blank(value):
        return None

    try:
        number = Decimal(str(value))

        if number != number.to_integral_value():
            return None

        return int(number)

    except Exception:
        return None


def normalized_name(value):
    return " ".join(
        str(value or "").split()
    ).casefold()


def parse_date(value):
    """
    Excel date cells are preferred.

    For text dates, use DD/MM/YYYY first because the
    source workbook uses day-first dates.
    """

    if blank(value):
        return None

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    text = str(value).strip()

    formats = (
        "%Y-%m-%d",
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%d/%m/%y",
        "%m/%d/%Y",
    )

    for fmt in formats:
        try:
            return datetime.strptime(
                text,
                fmt,
            ).date()

        except ValueError:
            continue

    try:
        return datetime.fromisoformat(text).date()

    except ValueError:
        return None


def historical_datetime(day):
    return datetime.combine(
        day,
        time.min,
    ).replace(tzinfo=TZ)


def normalize_payment(value):
    if blank(value):
        return None

    value = " ".join(
        str(value).strip().split()
    )

    known = {
        "cash": "Cash",
        "card": "Card",
        "upi": "UPI",
        "bank transfer": "Bank Transfer",
        "net banking": "Net Banking",
        "netbanking": "Net Banking",
        "cheque": "Cheque",
        "check": "Cheque",
    }

    return known.get(
        value.casefold(),
        value[:50],
    )


def normalize_reference(value):
    """
    Preserve transaction references as text.

    Do not convert a reference to an integer or float.
    """

    if blank(value):
        return None

    if isinstance(value, float):
        if value.is_integer():
            return format(int(value), "d")

        return format(value, ".15g")

    return str(value).strip()[:150]


# ------------------------------------------------------------
# EXCEL HELPERS
# ------------------------------------------------------------

def row_is_yellow(ws, row_number):
    for cell in ws[row_number]:

        fill = cell.fill

        if fill.fill_type != "solid":
            continue

        color = fill.fgColor

        if (
            color.type == "rgb"
            and color.rgb
            and color.rgb.upper() in YELLOW
        ):
            return True

    return False


def load_excel_rows(path):

    workbook = openpyxl.load_workbook(
        path,
        data_only=True,
    )

    ws = workbook.active

    headers = [
        str(cell.value).strip()
        if cell.value is not None
        else ""
        for cell in ws[1]
    ]

    required_columns = {
        "Patient_ID",
        "Patient_Name",
        "Bill_Type",
        "Bill_Date",
        "Doctor_ID",
        "Doctor_Name",
        "Department",
        "Service_Group_ID",
        "Service_Group_Name",
        "Service_ID",
        "Service_Name",
        "Quantity",
        "Rate",
        "Discount_Amount",
        "Bill_Discount_Amount",
        "Notes",
        "Amount_Paid",
        "Payment_Mode",
        "reference_number",
    }

    missing = sorted(
        required_columns - set(headers)
    )

    if missing:
        raise ValueError(
            "Missing Excel columns: "
            + ", ".join(missing)
        )

    rows = []

    for excel_row, values in enumerate(
        ws.iter_rows(
            min_row=2,
            values_only=True,
        ),
        start=2,
    ):

        if not any(value is not None for value in values):
            continue

        data = dict(
            zip(headers, values)
        )

        row = {
            **data,

            "_excel_row": excel_row,

            "_yellow": row_is_yellow(
                ws,
                excel_row,
            ),

            "_patient_id": (
                None
                if blank(data.get("Patient_ID"))
                else str(data["Patient_ID"]).strip()
            ),

            "_patient_name": " ".join(
                str(data.get("Patient_Name") or "").split()
            ),

            "_bill_date": parse_date(
                data.get("Bill_Date")
            ),

            "_bill_type": str(
                data.get("Bill_Type") or ""
            ).strip().upper(),

            "_doctor_id": as_int(
                data.get("Doctor_ID")
            ),

            "_service_id": as_int(
                data.get("Service_ID")
            ),

            "_group_id": as_int(
                data.get("Service_Group_ID")
            ),

            "_quantity": money(
                1
                if blank(data.get("Quantity"))
                else data.get("Quantity")
            ),

            "_rate": (
                None
                if blank(data.get("Rate"))
                else money(data.get("Rate"))
            ),

            "_item_discount": money(
                data.get("Discount_Amount")
            ),

            "_bill_discount": money(
                data.get("Bill_Discount_Amount")
            ),

            "_amount_paid": (
                None
                if blank(data.get("Amount_Paid"))
                else money(data.get("Amount_Paid"))
            ),

            "_payment_mode": normalize_payment(
                data.get("Payment_Mode")
            ),

            "_reference": normalize_reference(
                data.get("reference_number")
            ),

            "_special_note": (
                None
                if blank(data.get("Notes"))
                else str(data.get("Notes")).strip()
            ),
        }

        rows.append(row)

    return rows


# ------------------------------------------------------------
# BILL GROUPING
# ------------------------------------------------------------

def group_bills(rows, add_issue):

    groups = []
    current = None

    for row in rows:

        if row["_yellow"]:

            if current is None:
                add_issue(
                    row,
                    "ERROR",
                    "Yellow row has no preceding bill row.",
                )
                continue

            previous = current[0]

            if normalized_name(
                previous["_patient_name"]
            ) != normalized_name(
                row["_patient_name"]
            ):
                add_issue(
                    row,
                    "ERROR",
                    "Registration row patient differs from preceding row.",
                )

            if (
                previous["_bill_date"]
                != row["_bill_date"]
            ):
                add_issue(
                    row,
                    "ERROR",
                    "Registration row date differs from preceding row.",
                )

            current.append(row)

        else:

            current = [row]
            groups.append(current)

    return groups


# ------------------------------------------------------------
# CSV REPORTS
# ------------------------------------------------------------

ISSUE_HEADERS = [
    "excel_row",
    "patient_id",
    "patient_name",
    "bill_date",
    "service_id",
    "service_name",
    "severity",
    "issue",
]

REPORT_HEADERS = [
    "status",
    "excel_rows",
    "patient_name",
    "bill_date",
    "items",
    "bill_id",
    "bill_number",
    "op_number",
    "total",
    "payment",
    "message",
]


def write_csv(path, headers, rows):

    with Path(path).open(
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=headers,
            extrasaction="ignore",
        )

        writer.writeheader()
        writer.writerows(rows)


# ------------------------------------------------------------
# AUDIT HELPERS
# ------------------------------------------------------------

def json_safe(value):
    """
    Match the backend bill_audit_service JSON conversion.
    """

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    if isinstance(value, Decimal):
        return str(value)

    if isinstance(value, dict):
        return {
            str(key): json_safe(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple)):
        return [
            json_safe(item)
            for item in value
        ]

    return value


def write_bill_audit(
    cursor,
    *,
    bill_id,
    action,
    entity_type,
    entity_id=None,
    changed_by=None,
    old_values=None,
    new_values=None,
    reason=None,
):
    """
    Same insert structure as Services/bill_audit_service.py.

    changed_at is populated by the database default,
    exactly as the existing backend helper does.
    """

    cursor.execute(
        """
        INSERT INTO public.bill_audit_history (
            bill_id,
            action,
            entity_type,
            entity_id,
            old_values,
            new_values,
            changed_by,
            reason
        )
        VALUES (
            %s, %s, %s, %s,
            %s::jsonb, %s::jsonb,
            %s, %s
        )
        RETURNING id
        """,
        (
            bill_id,
            action,
            entity_type,
            entity_id,

            (
                json.dumps(
                    json_safe(old_values),
                    ensure_ascii=False,
                )
                if old_values is not None
                else None
            ),

            (
                json.dumps(
                    json_safe(new_values),
                    ensure_ascii=False,
                )
                if new_values is not None
                else None
            ),

            changed_by,
            reason,
        ),
    )

    result = cursor.fetchone()

    return result["id"]


def fetch_bill_items_for_audit(cursor, bill_id):

    cursor.execute(
        """
        SELECT *
        FROM public.bill_items
        WHERE bill_id = %s
        ORDER BY id
        """,
        (bill_id,),
    )

    return [
        dict(row)
        for row in cursor.fetchall()
    ]


def write_bill_item_diff(
    cursor,
    *,
    bill_id,
    old_items,
    new_items,
    changed_by,
):
    """
    Follow the backend write_bill_item_diff convention.

    For this import, all line items are new.
    """

    ignored_fields = {
        "id",
        "bill_id",
        "created_at",
        "updated_at",
        "room_type_id",
        "room_type_name",
        "bed_number",
    }

    for item in new_items:

        item_payload = {
            key: value
            for key, value in item.items()
            if key not in ignored_fields
        }

        write_bill_audit(
            cursor,
            bill_id=bill_id,
            action="bill_item.added",
            entity_type="bill_item",
            entity_id=item.get("id"),
            changed_by=changed_by,
            new_values=item_payload,
            reason="Bill item added.",
        )


# ------------------------------------------------------------
# SYSTEM USER
# ------------------------------------------------------------

def get_system_user(cursor):

    cursor.execute(
        """
        SELECT id, login_id, full_name
        FROM public.users
        WHERE LOWER(TRIM(login_id)) = 'system'
           OR LOWER(TRIM(full_name)) = 'system'
        ORDER BY
            CASE
                WHEN LOWER(TRIM(login_id)) = 'system'
                THEN 0
                ELSE 1
            END,
            id
        LIMIT 1
        """
    )

    user = cursor.fetchone()

    if not user:
        raise RuntimeError(
            "Existing System user was not found. "
            "Import stopped."
        )

    return user


# ------------------------------------------------------------
# DATABASE HELPERS
# ------------------------------------------------------------

def get_database_url():

    return (
        os.getenv("DATABASE_URL")
        or os.getenv("DATABASE_CONNECTION_STRING")
        or os.getenv("POSTGRES_CONNECTION_STRING")
        or os.getenv("POSTGRES_URL")
    )


def get_service(cursor, row):

    service_id = row["_service_id"]

    if service_id is not None:

        cursor.execute(
            """
            SELECT
                s.id,
                s.name,
                s.service_group_id,
                s.is_active,
                s.available_for_op,
                s.requires_special_note,
                sg.name AS group_name,
                sg.is_active AS group_active
            FROM public.services s
            JOIN public.service_groups sg
                ON sg.id = s.service_group_id
            WHERE s.id = %s
            """,
            (service_id,),
        )

        service = cursor.fetchone()

    else:

        cursor.execute(
            """
            SELECT
                s.id,
                s.name,
                s.service_group_id,
                s.is_active,
                s.available_for_op,
                s.requires_special_note,
                sg.name AS group_name,
                sg.is_active AS group_active
            FROM public.services s
            JOIN public.service_groups sg
                ON sg.id = s.service_group_id
            WHERE LOWER(TRIM(s.name)) =
                  LOWER(TRIM(%s))
              AND (
                    %s IS NULL
                    OR s.service_group_id = %s
              )
            ORDER BY s.id
            """,
            (
                str(row.get("Service_Name") or "").strip(),
                row["_group_id"],
                row["_group_id"],
            ),
        )

        matches = cursor.fetchall()

        if len(matches) != 1:
            raise ValueError(
                f"Excel row {row['_excel_row']}: "
                "Service ID is blank and exact service lookup "
                "is not unique."
            )

        service = matches[0]

    if not service:
        raise ValueError(
            f"Excel row {row['_excel_row']}: "
            f"Service ID {service_id} not found."
        )

    if not service["is_active"]:
        raise ValueError(
            f"Excel row {row['_excel_row']}: "
            "Service is inactive."
        )

    if not service["group_active"]:
        raise ValueError(
            f"Excel row {row['_excel_row']}: "
            "Service group is inactive."
        )

    if not service["available_for_op"]:
        raise ValueError(
            f"Excel row {row['_excel_row']}: "
            "Service is not available for OP billing."
        )

    if (
        row["_group_id"] is not None
        and row["_group_id"] != service["service_group_id"]
    ):
        raise ValueError(
            f"Excel row {row['_excel_row']}: "
            "Service group does not match."
        )

    return service


def find_existing_import_bill(
    cursor,
    source_tag,
    source_excel_row,
):
    """
    Duplicate protection uses audit JSON metadata.
    No import marker is written to bills.notes.
    """

    cursor.execute(
        """
        SELECT
            b.id,
            b.bill_number,
            b.op_number,
            b.total_amount
        FROM public.bills b
        JOIN public.bill_audit_history h
            ON h.bill_id = b.id
        WHERE h.action = 'bill.created'
          AND h.entity_type = 'bill'
          AND h.new_values->'import_metadata'->>'source_tag' = %s
          AND h.new_values->'import_metadata'->>'source_excel_row' = %s
        ORDER BY h.id
        LIMIT 1
        """,
        (
            source_tag,
            str(source_excel_row),
        ),
    )

    return cursor.fetchone()


def find_patient_created_by_this_import(
    cursor,
    source_tag,
    patient_name,
):
    """
    Reuse a name-only patient created by a previous run
    of this same workbook.
    """

    cursor.execute(
        """
        SELECT
            p.id,
            p.patient_id,
            p.name
        FROM public.bills b
        JOIN public.patients p
            ON p.id = b.patient_db_id
        JOIN public.bill_audit_history h
            ON h.bill_id = b.id
        WHERE h.action = 'bill.created'
          AND h.entity_type = 'bill'
          AND h.new_values->'import_metadata'->>'source_tag' = %s
          AND LOWER(TRIM(p.name)) = LOWER(TRIM(%s))
        ORDER BY b.id
        LIMIT 1
        """,
        (
            source_tag,
            patient_name,
        ),
    )

    return cursor.fetchone()


def get_or_create_patient(
    cursor,
    items,
    source_tag,
    patient_cache,
):

    patient_ids = {
        row["_patient_id"]
        for row in items
        if row["_patient_id"]
    }

    if len(patient_ids) > 1:
        raise ValueError(
            "Grouped bill rows contain multiple Patient_ID values."
        )

    supplied_patient_id = next(
        iter(patient_ids),
        None,
    )

    if supplied_patient_id:

        cursor.execute(
            """
            SELECT id, patient_id, name
            FROM public.patients
            WHERE patient_id = %s
            FOR SHARE
            """,
            (supplied_patient_id,),
        )

        patient = cursor.fetchone()

        if not patient:
            raise ValueError(
                f"Patient_ID {supplied_patient_id} "
                "does not exist. No replacement patient was created."
            )

        return patient

    patient_name = items[0]["_patient_name"]

    cache_key = normalized_name(patient_name)

    if cache_key in patient_cache:
        return patient_cache[cache_key]

    patient = find_patient_created_by_this_import(
        cursor,
        source_tag,
        patient_name,
    )

    if patient:
        patient_cache[cache_key] = patient
        return patient

    generated_patient_id = (
        "PAT-" + uuid.uuid4().hex[:12].upper()
    )

    # Only the patient name and generated patient ID are populated.
    # Other demographic fields remain empty.
    cursor.execute(
        """
        INSERT INTO public.patients (
            patient_id,
            name,
            mobile,
            address,
            pincode
        )
        VALUES (%s, %s, '', '', '')
        RETURNING id, patient_id, name
        """,
        (
            generated_patient_id,
            patient_name,
        ),
    )

    patient = cursor.fetchone()

    patient_cache[cache_key] = patient

    return patient


# ------------------------------------------------------------
# VALIDATE ONE BILL
# ------------------------------------------------------------

def prepare_bill(
    cursor,
    items,
):

    first = items[0]

    patient_name = first["_patient_name"]
    bill_date = first["_bill_date"]

    if not patient_name:
        raise ValueError("Patient name is blank.")

    if not bill_date:
        raise ValueError("Bill date is missing or invalid.")

    if first["_bill_type"] != "OP":
        raise ValueError(
            "Only OP bills are allowed in this import."
        )

    if len({
        row["_bill_date"]
        for row in items
    }) != 1:
        raise ValueError(
            "Grouped rows have different bill dates."
        )

    if len({
        normalized_name(row["_patient_name"])
        for row in items
    }) != 1:
        raise ValueError(
            "Grouped rows have different patient names."
        )

    prepared_items = []

    for row in items:

        if row["_quantity"] <= 0:
            raise ValueError(
                f"Excel row {row['_excel_row']}: "
                "Quantity must be greater than zero."
            )

        if row["_rate"] is None:
            raise ValueError(
                f"Excel row {row['_excel_row']}: "
                "Rate is blank. Database pricing will not be used."
            )

        if row["_rate"] < 0:
            raise ValueError(
                f"Excel row {row['_excel_row']}: "
                "Rate cannot be negative."
            )

        if (
            row["_item_discount"] < 0
            or row["_bill_discount"] < 0
        ):
            raise ValueError(
                f"Excel row {row['_excel_row']}: "
                "Discount cannot be negative."
            )

        service = get_service(cursor, row)

        special_note = row["_special_note"]

        if (
            service["requires_special_note"]
            and not special_note
        ):
            raise ValueError(
                f"Excel row {row['_excel_row']}: "
                "Required service note is missing."
            )

        quantity = row["_quantity"]

        # IMPORTANT:
        # The Excel rate is always used.
        # Do not query service_prices for a replacement rate.
        rate = row["_rate"]

        amount = money(rate * quantity)

        discount = row["_item_discount"]

        if discount > amount:
            raise ValueError(
                f"Excel row {row['_excel_row']}: "
                "Discount exceeds line amount."
            )

        net_amount = money(
            amount - discount
        )

        prepared_items.append({
            "row": row,
            "service": service,
            "rate": rate,
            "quantity": quantity,
            "amount": amount,
            "discount": discount,
            "net_amount": net_amount,
            "special_note": special_note,
        })

    bill_discount_values = {
        row["_bill_discount"]
        for row in items
        if not blank(row.get("Bill_Discount_Amount"))
    }

    if len(bill_discount_values) > 1:
        raise ValueError(
            "Conflicting bill discounts within one bill."
        )

    bill_discount = next(
        iter(bill_discount_values),
        Decimal("0.00"),
    )

    subtotal = money(
        sum(
            (
                item["amount"]
                for item in prepared_items
            ),
            Decimal("0.00"),
        )
    )

    item_discount_total = money(
        sum(
            (
                item["discount"]
                for item in prepared_items
            ),
            Decimal("0.00"),
        )
    )

    if bill_discount > (
        subtotal - item_discount_total
    ):
        raise ValueError(
            "Bill discount exceeds the remaining bill amount."
        )

    total = money(
        subtotal
        - item_discount_total
        - bill_discount
    )

    # Payment information belongs to the bill's primary row.
    payment_rows = [
        row
        for row in items
        if row["_amount_paid"] is not None
    ]

    payment_values = {
        (
            row["_amount_paid"],
            row["_payment_mode"],
            row["_reference"],
        )
        for row in payment_rows
    }

    if len(payment_values) > 1:
        raise ValueError(
            "Conflicting payment details within one bill."
        )

    if not payment_values:
        raise ValueError(
            "Payment amount is missing."
        )

    paid, payment_method, reference = next(
        iter(payment_values)
    )

    if paid <= 0:
        raise ValueError(
            "Payment amount must be greater than zero."
        )

    if paid > total:
        raise ValueError(
            f"Payment amount {paid} exceeds bill total {total}."
        )

    return {
        "prepared_items": prepared_items,
        "subtotal": subtotal,
        "item_discount_total": item_discount_total,
        "bill_discount": bill_discount,
        "total": total,
        "paid": paid,
        "payment_method": payment_method or "Unknown",
        "reference": reference,
        "bill_date": bill_date,
    }


# ------------------------------------------------------------
# CREATE BILL, ITEMS, PAYMENT AND AUDIT
# ------------------------------------------------------------

def import_one_bill(
    cursor,
    items,
    source_tag,
    system_user_id,
    patient_cache,
):

    first = items[0]

    source_excel_row = first["_excel_row"]

    # 1. Check duplicate using audit metadata.
    existing = find_existing_import_bill(
        cursor,
        source_tag,
        source_excel_row,
    )

    if existing:
        return {
            "status": "SKIPPED_DUPLICATE",
            "bill_id": existing["id"],
            "bill_number": existing["bill_number"],
            "op_number": existing["op_number"],
            "total": str(existing["total_amount"]),
            "payment": "",
            "message": "This source bill was already imported.",
        }

    # 2. Validate and calculate using Excel rates.
    calculation = prepare_bill(
        cursor,
        items,
    )

    # 3. Resolve or create patient.
    patient = get_or_create_patient(
        cursor,
        items,
        source_tag,
        patient_cache,
    )

    # 4. Validate doctor.
    doctor_id = first["_doctor_id"]
    doctor_name = str(
        first.get("Doctor_Name") or ""
    ).strip() or None

    if doctor_id is not None:

        cursor.execute(
            """
            SELECT id, name
            FROM public.doctors
            WHERE id = %s
            """,
            (doctor_id,),
        )

        doctor = cursor.fetchone()

        if not doctor:
            raise ValueError(
                f"Doctor ID {doctor_id} does not exist."
            )

        doctor_name = doctor["name"]

    historical_dt = historical_datetime(
        calculation["bill_date"]
    )

    # 5. Create bill.
    #
    # Bill Notes are deliberately NULL.
    # No import marker is stored in visible bill notes.
    cursor.execute(
        """
        INSERT INTO public.bills (
            bill_type,
            patient_db_id,
            bill_date,
            department,
            doctor_id,
            doctor_name,
            subtotal,
            discount_amount,
            total_amount,
            notes,
            created_by
        )
        VALUES (
            'OP',
            %s, %s, %s, %s, %s,
            %s, %s, %s, NULL, %s
        )
        RETURNING *
        """,
        (
            patient["id"],
            historical_dt,
            str(
                first.get("Department") or ""
            )[:100] or None,
            doctor_id,
            doctor_name,
            calculation["subtotal"],
            money(
                calculation["item_discount_total"]
                + calculation["bill_discount"]
            ),
            calculation["total"],
            system_user_id,
        ),
    )

    bill = dict(cursor.fetchone())

    # 6. Create bill items using Excel rates.
    for item in calculation["prepared_items"]:

        row = item["row"]
        service = item["service"]

        cursor.execute(
            """
            INSERT INTO public.bill_items (
                bill_id,
                service_id,
                service_group_id,
                service_price_id,
                service_name,
                service_group_name,
                service_date,
                special_note,
                rate,
                quantity,
                amount,
                discount_amount,
                net_amount,
                gst_amount
            )
            VALUES (
                %s, %s, %s, NULL, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, 0
            )
            RETURNING id
            """,
            (
                bill["id"],
                service["id"],
                service["service_group_id"],
                service["name"],
                service["group_name"],
                calculation["bill_date"],
                item["special_note"],
                item["rate"],
                item["quantity"],
                item["amount"],
                item["discount"],
                item["net_amount"],
            ),
        )

        # Item is inserted here.
        # Its audit entry is written after all items are fetched.

    # 7. Fetch complete bill items for audit snapshot.
    created_items = fetch_bill_items_for_audit(
        cursor,
        bill["id"],
    )

    # 8. Bill creation audit.
    #
    # This follows billing_service.py:
    # action = bill.created
    # new_values contains bill and items.
    #
    # Import metadata is stored inside audit JSON for
    # duplicate detection without polluting Bill Notes.
    write_bill_audit(
        cursor,
        bill_id=bill["id"],
        action="bill.created",
        entity_type="bill",
        entity_id=bill["id"],
        changed_by=system_user_id,
        new_values={
            "bill": dict(bill),
            "items": created_items,
            "import_metadata": {
                "source_tag": source_tag,
                "source_excel_row": str(source_excel_row),
                "excel_rows": [
                    row["_excel_row"]
                    for row in items
                ],
            },
        },
    )

    # 9. Individual bill item audit entries.
    write_bill_item_diff(
        cursor,
        bill_id=bill["id"],
        old_items=[],
        new_items=created_items,
        changed_by=system_user_id,
    )

    # 10. Bill total audit.
    write_bill_audit(
        cursor,
        bill_id=bill["id"],
        action="bill.totals_updated",
        entity_type="bill",
        entity_id=bill["id"],
        changed_by=system_user_id,
        old_values={
            "subtotal": "0.00",
            "total_amount": "0.00",
        },
        new_values={
            "subtotal": str(calculation["subtotal"]),
            "total_amount": str(calculation["total"]),
        },
        reason="Bill totals calculated from the imported bill items.",
    )

    # 11. Create payment.
    #
    # Payment Notes are deliberately NULL.
    # created_by is the existing System user.
    # created_at is left to the database default.
    cursor.execute(
        """
        INSERT INTO public.bill_payments (
            bill_id,
            amount,
            payment_date,
            payment_method,
            reference_number,
            notes,
            created_by
        )
        VALUES (
            %s, %s, %s, %s, %s, NULL, %s
        )
        RETURNING *
        """,
        (
            bill["id"],
            calculation["paid"],
            historical_dt,
            calculation["payment_method"],
            calculation["reference"],
            system_user_id,
        ),
    )

    payment = dict(cursor.fetchone())


    # 13. Update patient cache only after successful transaction.
    patient_cache[
        normalized_name(patient["name"])
    ] = patient

    return {
    "status": "IMPORTED",
    "bill_id": bill["id"],
    "bill_number": bill.get("bill_number"),
    "op_number": bill.get("op_number"),
    "total": str(calculation["total"]),
    "payment": str(calculation["paid"]),
    "message": "Bill, items and payment created successfully.",
}


# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------

def main():

    parser = argparse.ArgumentParser(
        description="Import historical OP bills from Excel."
    )

    parser.add_argument(
        "--excel",
        default=str(DEFAULT_EXCEL),
    )

    parser.add_argument(
        "--plan-only",
        action="store_true",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
    )

    parser.add_argument(
        "--issues",
        default=ISSUE_FILE,
    )

    parser.add_argument(
        "--report",
        default=REPORT_FILE,
    )

    args = parser.parse_args()

    excel_path = Path(args.excel).resolve()

    if not excel_path.exists():
        raise SystemExit(
            f"Excel file not found: {excel_path}"
        )

    # Unique source tag based on workbook content.
    source_hash = hashlib.sha256(
        excel_path.read_bytes()
    ).hexdigest()[:16].upper()

    source_tag = f"HISTOP-{source_hash}"

    logger.info(
        "Starting historical OP bill import. Source=%s",
        source_tag,
    )

    rows = load_excel_rows(excel_path)

    issues = []
    report = []

    def add_issue(row, severity, message):

        issues.append({
            "excel_row": row.get("_excel_row", ""),
            "patient_id": row.get("_patient_id") or "",
            "patient_name": row.get("_patient_name") or "",
            "bill_date": str(
                row.get("_bill_date") or ""
            ),
            "service_id": row.get("_service_id") or "",
            "service_name": row.get("Service_Name") or "",
            "severity": severity,
            "issue": message,
        })

    groups = group_bills(
        rows,
        add_issue,
    )

    logger.info(
        "Rows=%d | Yellow rows=%d | Candidate bills=%d",
        len(rows),
        sum(1 for row in rows if row["_yellow"]),
        len(groups),
    )

    # Strict 63 bill validation.
    if len(groups) != EXPECTED_BILLS:

        add_issue(
            {},
            "ERROR",
            (
                f"Expected {EXPECTED_BILLS} bills, "
                f"but detected {len(groups)}."
            ),
        )

        write_csv(
            args.issues,
            ISSUE_HEADERS,
            issues,
        )

        write_csv(
            args.report,
            REPORT_HEADERS,
            report,
        )

        raise SystemExit(
            f"STOP: expected 63 bills, found {len(groups)}."
        )

    # Initial workbook-level validation.
    for group_no, items in enumerate(
        groups,
        start=1,
    ):

        first = items[0]

        rows_label = ",".join(
            str(row["_excel_row"])
            for row in items
        )

        report.append({
            "status": "PLANNED" if args.plan_only else "PENDING",
            "excel_rows": rows_label,
            "patient_name": first["_patient_name"],
            "bill_date": str(first["_bill_date"] or ""),
            "items": len(items),
            "bill_id": "",
            "bill_number": "",
            "op_number": "",
            "total": "",
            "payment": "",
            "message": f"Candidate bill {group_no}",
        })

        for row in items:

            if not row["_patient_name"]:
                add_issue(
                    row,
                    "ERROR",
                    "Patient name is blank.",
                )

            if not row["_bill_date"]:
                add_issue(
                    row,
                    "ERROR",
                    "Bill date is missing or invalid.",
                )

            if row["_bill_type"] != "OP":
                add_issue(
                    row,
                    "ERROR",
                    "Bill type must be OP.",
                )

            if row["_rate"] is None:
                add_issue(
                    row,
                    "ERROR",
                    "Excel rate is blank. Database rate will not be used.",
                )

            if row["_quantity"] <= 0:
                add_issue(
                    row,
                    "ERROR",
                    "Quantity must be greater than zero.",
                )

    # Plan-only mode does not connect to the database.
    if args.plan_only:

        write_csv(
            args.issues,
            ISSUE_HEADERS,
            issues,
        )

        write_csv(
            args.report,
            REPORT_HEADERS,
            report,
        )

        logger.info(
            "Plan-only completed. Candidate bills=%d",
            len(groups),
        )

        return 1 if any(
            issue["severity"] == "ERROR"
            for issue in issues
        ) else 0

    if psycopg2 is None:
        raise SystemExit(
            "psycopg2 is not installed. "
            "Install the backend requirements first."
        )

    db_url = get_database_url()

    if not db_url:
        raise SystemExit(
            "Database URL not found in .env."
        )

    connection = None
    patient_cache = {}

    try:

        connection = psycopg2.connect(
            db_url,
            options="-c timezone=Asia/Kolkata",
            connect_timeout=10,
            cursor_factory=RealDictCursor,
        )

        # Find the existing System user.
        with connection.cursor() as cursor:

            system_user = get_system_user(cursor)

        system_user_id = system_user["id"]

        logger.info(
            "Using existing System user. ID=%s",
            system_user_id,
        )

        for items in groups:

            first = items[0]

            rows_label = ",".join(
                str(row["_excel_row"])
                for row in items
            )

            label = (
                f"{first['_patient_name']} | "
                f"{first['_bill_date']} | "
                f"Excel rows {rows_label}"
            )

            report_row = next(
                row
                for row in report
                if row["excel_rows"] == rows_label
            )

            try:

                if args.dry_run:

                    # Validate without creating database records.
                    with connection.cursor() as cursor:

                        patient_ids = {
                            row["_patient_id"]
                            for row in items
                            if row["_patient_id"]
                        }

                        if len(patient_ids) > 1:
                            raise ValueError(
                                "Grouped rows contain multiple Patient_ID values."
                            )

                        prepare_bill(
                            cursor,
                            items,
                        )

                        supplied_pid = next(
                            iter(patient_ids),
                            None,
                        )

                        if supplied_pid:

                            cursor.execute(
                                """
                                SELECT id
                                FROM public.patients
                                WHERE patient_id = %s
                                """,
                                (supplied_pid,),
                            )

                            if not cursor.fetchone():
                                raise ValueError(
                                    f"Patient_ID {supplied_pid} not found."
                                )

                    # Dry run has no inserts.
                    report_row.update({
                        "status": "DRY_RUN_OK",
                        "message": (
                            "Validation passed. No records inserted."
                        ),
                    })

                    logger.info(
                        "DRY RUN OK | %s",
                        label,
                    )

                    continue

                # Each bill is its own transaction.
                with connection:

                    with connection.cursor() as cursor:

                        result = import_one_bill(
                            cursor=cursor,
                            items=items,
                            source_tag=source_tag,
                            system_user_id=system_user_id,
                            patient_cache=patient_cache,
                        )

                report_row.update(result)

                logger.info(
                    "%s | status=%s | bill_id=%s | OP=%s | total=%s",
                    label,
                    result["status"],
                    result.get("bill_id"),
                    result.get("op_number"),
                    result.get("total"),
                )

            except Exception as exc:

                connection.rollback()

                error_message = (
                    f"{type(exc).__name__}: {exc}"
                )

                for row in items:
                    add_issue(
                        row,
                        "ERROR",
                        error_message,
                    )

                report_row.update({
                    "status": "FAILED",
                    "message": error_message,
                })

                logger.exception(
                    "Failed to import bill group: %s",
                    label,
                )

    except Exception:

        logger.exception(
            "Fatal historical OP import error."
        )

        raise

    finally:

        if connection is not None:
            connection.close()

        write_csv(
            args.issues,
            ISSUE_HEADERS,
            issues,
        )

        write_csv(
            args.report,
            REPORT_HEADERS,
            report,
        )

        logger.info(
            "Issue report saved: %s",
            Path(args.issues).resolve(),
        )

        logger.info(
            "Import report saved: %s",
            Path(args.report).resolve(),
        )

    imported = sum(
        1
        for row in report
        if row["status"] == "IMPORTED"
    )

    skipped = sum(
        1
        for row in report
        if row["status"] == "SKIPPED_DUPLICATE"
    )

    failed = sum(
        1
        for row in report
        if row["status"] == "FAILED"
    )

    dry_run_ok = sum(
        1
        for row in report
        if row["status"] == "DRY_RUN_OK"
    )

    logger.info(
        (
            "Import completed | total=%d | imported=%d | "
            "duplicate_skipped=%d | dry_run_ok=%d | failed=%d"
        ),
        len(groups),
        imported,
        skipped,
        dry_run_ok,
        failed,
    )

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())