
#!/usr/bin/env python3

"""
BULK PATIENT IMPORTER
=====================

Excel:
    IP MANUAL BILL PATIENT DETAILS.xlsx

Database table:
    public.patients

Fields inserted:
    patient_id
    name
    mobile
    address
    pincode

Duplicate rules:
    1. Same name + same mobile:
       DUPLICATE

    2. Same name + different mobile:
       POSSIBLE_DUPLICATE

    3. Same name + blank mobile:
       POSSIBLE_DUPLICATE

    4. Different name + same mobile:
       DUPLICATE

    5. Different name + different mobile:
       NEW PATIENT

Duplicate checks are performed against:
    - Existing PostgreSQL patients
    - Other records in the Excel file

Commands:
    python patients.py --plan-only
    python patients.py --dry-run
    python patients.py

No bills, payments or audit history are created.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import uuid

from pathlib import Path

import openpyxl

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ImportError:
    psycopg2 = None
    RealDictCursor = None


# ============================================================
# CONFIGURATION
# ============================================================

SCRIPT_DIR = Path(__file__).resolve().parent

# Expected structure:
# backend/
#   Bulk imports/
#       Bulk Patients imports/
#           patients.py

BACKEND_DIR = SCRIPT_DIR.parents[1]

DEFAULT_EXCEL = (
    SCRIPT_DIR / "IP MANUAL BILL PATIENT DETAILS.xlsx"
)

REPORT_FILE = SCRIPT_DIR / "patient_import_report.csv"
ISSUE_FILE = SCRIPT_DIR / "patient_import_issues.csv"
LOG_FILE = SCRIPT_DIR / "patient_import.log"

CONNECTION_ENV_NAMES = [
    "DATABASE_URL",
    "DATABASE_CONNECTION_STRING",
    "POSTGRES_CONNECTION_STRING",
    "POSTGRES_URL",
]

REQUIRED_COLUMNS = {
    "NAME",
    "MOB NO",
    "ADDRESS",
}


# ============================================================
# ENVIRONMENT
# ============================================================

if load_dotenv:
    env_file = BACKEND_DIR / ".env"

    if env_file.exists():
        load_dotenv(env_file, override=False)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            LOG_FILE,
            encoding="utf-8",
        ),
    ],
)

logger = logging.getLogger("patient_import")


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_database_url():

    for env_name in CONNECTION_ENV_NAMES:

        value = os.getenv(env_name)

        if value and value.strip():
            return value.strip()

    return None


def get_db_connection():

    if psycopg2 is None:
        raise RuntimeError(
            "psycopg2 is not installed. "
            "Install the backend requirements."
        )

    database_url = get_database_url()

    if not database_url:
        raise RuntimeError(
            "Database connection string not found. "
            "Check the backend .env file."
        )

    return psycopg2.connect(
        database_url,
        options="-c timezone=Asia/Kolkata",
        connect_timeout=10,
        cursor_factory=RealDictCursor,
    )


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def clean_text(value):

    if value is None:
        return ""

    return str(value).strip()


def normalize_name(value):
    """
    Normalize patient names for duplicate comparison.

    Examples:

        MOHAMMED RAFI
        Mohammed Rafi
        mohammed   rafi
        Mohammed-Rafi

    All normalize to the same comparison value.
    """

    if value is None:
        return ""

    name = str(value).strip().casefold()

    # Remove punctuation and symbols.
    name = re.sub(
        r"[^\w\s]",
        "",
        name,
    )

    # Normalize repeated spaces.
    name = re.sub(
        r"\s+",
        " ",
        name,
    )

    return name.strip()


def clean_mobile(value):
    """
    Normalize Excel mobile values.

    Examples:
        9876543210
        9876543210.0
        ' 9876543210 '
    """

    if value is None:
        return ""

    if isinstance(value, float) and value.is_integer():
        value = int(value)

    mobile = str(value).strip()

    if mobile.endswith(".0"):
        mobile = mobile[:-2]

    mobile = re.sub(
        r"[\s().-]",
        "",
        mobile,
    )

    return mobile


def normalize_mobile(value):

    mobile = clean_mobile(value)

    return re.sub(
        r"\D",
        "",
        mobile,
    )


def extract_pincode(address):

    address = clean_text(address)

    if not address:
        return ""

    matches = re.findall(
        r"(?<!\d)(\d{6})(?!\d)",
        address,
    )

    if matches:
        return matches[-1]

    return ""


def generate_patient_id():

    return (
        "PAT-"
        + uuid.uuid4().hex[:12].upper()
    )


def normalize_header(value):

    return re.sub(
        r"\s+",
        " ",
        str(value or "").strip().upper(),
    )


# ============================================================
# EXCEL LOADER
# ============================================================

def load_excel(excel_path):

    if not excel_path.exists():
        raise FileNotFoundError(
            f"Excel file not found: {excel_path}"
        )

    logger.info(
        "Reading Excel file: %s",
        excel_path,
    )

    workbook = openpyxl.load_workbook(
        excel_path,
        data_only=True,
    )

    worksheet = workbook.active

    if worksheet.max_row < 2:
        raise ValueError(
            "Excel file does not contain patient records."
        )

    headers = {}

    for column_number in range(
        1,
        worksheet.max_column + 1,
    ):

        header_value = worksheet.cell(
            row=1,
            column=column_number,
        ).value

        header = normalize_header(header_value)

        if header:
            headers[header] = column_number

    missing_columns = (
        REQUIRED_COLUMNS - set(headers.keys())
    )

    if missing_columns:
        raise ValueError(
            "Missing required Excel columns: "
            + ", ".join(sorted(missing_columns))
        )

    patients = []

    for row_number in range(
        2,
        worksheet.max_row + 1,
    ):

        name = clean_text(
            worksheet.cell(
                row=row_number,
                column=headers["NAME"],
            ).value
        )

        mobile = clean_mobile(
            worksheet.cell(
                row=row_number,
                column=headers["MOB NO"],
            ).value
        )

        address = clean_text(
            worksheet.cell(
                row=row_number,
                column=headers["ADDRESS"],
            ).value
        )

        # Skip completely empty rows.
        if not name and not mobile and not address:
            continue

        patients.append({

            "excel_row": row_number,

            "name": name,

            "normalized_name": normalize_name(name),

            "mobile": mobile,

            "normalized_mobile": normalize_mobile(mobile),

            "address": address,

            "pincode": extract_pincode(address),

        })

    workbook.close()

    logger.info(
        "Total Excel patient records: %s",
        len(patients),
    )

    return patients


# ============================================================
# VALIDATION
# ============================================================

def validate_patient(patient):

    errors = []

    if not patient["name"]:

        errors.append(
            "Patient name is missing."
        )

    elif len(patient["name"]) > 201:

        errors.append(
            "Patient name exceeds 201 characters."
        )

    mobile = patient["mobile"]

    if mobile and not re.fullmatch(
        r"[0-9+]{5,25}",
        mobile,
    ):

        errors.append(
            "Mobile number contains invalid characters "
            "or has an invalid length."
        )

    if len(patient["address"]) > 1000:

        errors.append(
            "Address exceeds 1000 characters."
        )

    return errors


# ============================================================
# EXISTING DATABASE PATIENTS
# ============================================================

def load_existing_patients(cursor):
    """
    Load existing patients from PostgreSQL.

    Build two lookup dictionaries:

        mobile_map:
            Normalized mobile -> patient records

        name_map:
            Normalized name -> patient records
    """

    cursor.execute(
        """
        SELECT
            id,
            patient_id,
            name,
            mobile
        FROM public.patients
        """
    )

    records = cursor.fetchall()

    mobile_map = {}
    name_map = {}

    for record in records:

        name_key = normalize_name(
            record.get("name")
        )

        mobile_key = normalize_mobile(
            record.get("mobile")
        )

        if name_key:

            name_map.setdefault(
                name_key,
                [],
            ).append(record)

        if mobile_key:

            mobile_map.setdefault(
                mobile_key,
                [],
            ).append(record)

    logger.info(
        "Existing database patients loaded: %s",
        len(records),
    )

    return {
        "mobile_map": mobile_map,
        "name_map": name_map,
    }


# ============================================================
# DUPLICATE CHECKER
# ============================================================

def check_duplicate(
    patient,
    existing_maps,
    seen_excel,
):
    """
    Returns:

        DUPLICATE
        POSSIBLE_DUPLICATE
        None

    Rules:

        Same mobile + same name:
            DUPLICATE

        Same mobile + different name:
            DUPLICATE

        Same name + different mobile:
            POSSIBLE_DUPLICATE

        Same name + blank mobile:
            POSSIBLE_DUPLICATE

        No match:
            None
    """

    name_key = patient["normalized_name"]

    mobile_key = patient["normalized_mobile"]

    mobile_matches = []
    name_matches = []

    # --------------------------------------------------------
    # DATABASE MATCHES
    # --------------------------------------------------------

    if mobile_key:

        mobile_matches = (
            existing_maps["mobile_map"].get(
                mobile_key,
                [],
            )
        )

    if name_key:

        name_matches = (
            existing_maps["name_map"].get(
                name_key,
                [],
            )
        )

    # --------------------------------------------------------
    # EXCEL MATCHES
    # --------------------------------------------------------

    if mobile_key:

        excel_mobile_matches = (
            seen_excel["mobile_map"].get(
                mobile_key,
                [],
            )
        )

        mobile_matches = (
            mobile_matches + excel_mobile_matches
        )

    if name_key:

        excel_name_matches = (
            seen_excel["name_map"].get(
                name_key,
                [],
            )
        )

        name_matches = (
            name_matches + excel_name_matches
        )

    # --------------------------------------------------------
    # PRIORITY 1: SAME MOBILE
    # --------------------------------------------------------

    if mobile_matches:

        matching_names = [
            normalize_name(
                record.get("name")
            )
            for record in mobile_matches
        ]

        if name_key in matching_names:

            return {
                "status": "DUPLICATE",
                "message": (
                    "Same patient name and mobile number "
                    "already exist."
                ),
            }

        return {
            "status": "DUPLICATE",
            "message": (
                "Mobile number already belongs to "
                "another patient record."
            ),
        }

    # --------------------------------------------------------
    # PRIORITY 2: SAME NAME, DIFFERENT MOBILE
    # --------------------------------------------------------

    if name_matches:

        return {
            "status": "POSSIBLE_DUPLICATE",
            "message": (
                "Patient name already exists, but the "
                "mobile number is different or blank. "
                "Manual verification required."
            ),
        }

    return None


# ============================================================
# UPDATE IN-MEMORY LOOKUPS
# ============================================================

def add_to_lookup_maps(
    patient_record,
    existing_maps,
):

    name_key = normalize_name(
        patient_record.get("name")
    )

    mobile_key = normalize_mobile(
        patient_record.get("mobile")
    )

    if name_key:

        existing_maps["name_map"].setdefault(
            name_key,
            [],
        ).append(patient_record)

    if mobile_key:

        existing_maps["mobile_map"].setdefault(
            mobile_key,
            [],
        ).append(patient_record)


def add_to_excel_seen(
    patient,
    seen_excel,
):

    name_key = patient["normalized_name"]

    mobile_key = patient["normalized_mobile"]

    record = {
        "name": patient["name"],
        "mobile": patient["mobile"],
    }

    if name_key:

        seen_excel["name_map"].setdefault(
            name_key,
            [],
        ).append(record)

    if mobile_key:

        seen_excel["mobile_map"].setdefault(
            mobile_key,
            [],
        ).append(record)


def create_empty_lookup():

    return {
        "name_map": {},
        "mobile_map": {},
    }


# ============================================================
# INSERT PATIENT
# ============================================================

def insert_patient(cursor, patient):

    patient_id = generate_patient_id()

    cursor.execute(
        """
        INSERT INTO public.patients (
            patient_id,
            name,
            mobile,
            address,
            pincode
        )
        VALUES (
            %s,
            %s,
            %s,
            %s,
            %s
        )
        RETURNING
            id,
            patient_id,
            name,
            mobile
        """,
        (
            patient_id,
            patient["name"],
            patient["mobile"],
            patient["address"],
            patient["pincode"],
        ),
    )

    return cursor.fetchone()


# ============================================================
# REPORT GENERATION
# ============================================================

def write_csv_report(
    file_path,
    rows,
    fieldnames,
):

    with open(
        file_path,
        "w",
        newline="",
        encoding="utf-8-sig",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=fieldnames,
            extrasaction="ignore",
        )

        writer.writeheader()

        writer.writerows(rows)

    logger.info(
        "Report generated: %s",
        file_path,
    )


# ============================================================
# PLAN ONLY
# ============================================================

def run_plan_only(patients):

    report_rows = []
    issue_rows = []

    seen_excel = create_empty_lookup()

    counts = {
        "READY": 0,
        "DUPLICATE": 0,
        "POSSIBLE_DUPLICATE": 0,
        "INVALID": 0,
    }

    for patient in patients:

        errors = validate_patient(patient)

        if errors:

            status = "INVALID"
            message = " | ".join(errors)

        else:

            duplicate = check_duplicate(
                patient,
                create_empty_lookup(),
                seen_excel,
            )

            if duplicate:

                status = duplicate["status"]
                message = duplicate["message"]

            else:

                if not patient["normalized_mobile"]:

                    status = "INVALID"
                    message = (
                        "Mobile number is missing and "
                        "no matching name was found."
                    )

                else:

                    status = "READY"
                    message = "Ready for database duplicate check."

        counts[status] += 1

        report_rows.append({
            "excel_row": patient["excel_row"],
            "patient_name": patient["name"],
            "mobile": patient["mobile"],
            "address": patient["address"],
            "pincode": patient["pincode"],
            "status": status,
            "patient_id": "",
            "message": message,
        })

        if status in (
            "INVALID",
            "DUPLICATE",
            "POSSIBLE_DUPLICATE",
        ):

            issue_rows.append({
                "excel_row": patient["excel_row"],
                "patient_name": patient["name"],
                "mobile": patient["mobile"],
                "issue": message,
            })

        # Add valid and duplicate records to the lookup
        # so later Excel rows are compared against them.
        if not errors:
            add_to_excel_seen(
                patient,
                seen_excel,
            )

    save_reports(
        report_rows,
        issue_rows,
    )

    print_summary(
        total=len(patients),
        counts=counts,
        mode="PLAN ONLY",
    )

    print(
        "\nNote: Plan-only checks duplicates within Excel. "
        "It does not connect to PostgreSQL."
    )


# ============================================================
# REPORT SAVING
# ============================================================

def save_reports(report_rows, issue_rows):

    report_fields = [
        "excel_row",
        "patient_name",
        "mobile",
        "address",
        "pincode",
        "status",
        "patient_id",
        "message",
    ]

    issue_fields = [
        "excel_row",
        "patient_name",
        "mobile",
        "issue",
    ]

    write_csv_report(
        REPORT_FILE,
        report_rows,
        report_fields,
    )

    write_csv_report(
        ISSUE_FILE,
        issue_rows,
        issue_fields,
    )


# ============================================================
# MAIN IMPORT
# ============================================================

def run_import(
    excel_path,
    dry_run=False,
    plan_only=False,
):

    patients = load_excel(excel_path)

    if plan_only:

        run_plan_only(patients)
        return

    connection = None

    report_rows = []
    issue_rows = []

    counts = {
        "INSERTED": 0,
        "WOULD_INSERT": 0,
        "DUPLICATE": 0,
        "POSSIBLE_DUPLICATE": 0,
        "INVALID": 0,
        "FAILED": 0,
    }

    seen_excel = create_empty_lookup()

    try:

        connection = get_db_connection()

        logger.info(
            "Database connection successful."
        )

        with connection.cursor() as cursor:

            existing_maps = load_existing_patients(
                cursor
            )

            for patient in patients:

                result = {
                    "excel_row": patient["excel_row"],
                    "patient_name": patient["name"],
                    "mobile": patient["mobile"],
                    "address": patient["address"],
                    "pincode": patient["pincode"],
                    "status": "",
                    "patient_id": "",
                    "message": "",
                }

                # ------------------------------------------------
                # VALIDATION
                # ------------------------------------------------

                errors = validate_patient(patient)

                if errors:

                    result["status"] = "INVALID"
                    result["message"] = " | ".join(errors)

                    counts["INVALID"] += 1

                    issue_rows.append({
                        "excel_row": patient["excel_row"],
                        "patient_name": patient["name"],
                        "mobile": patient["mobile"],
                        "issue": result["message"],
                    })

                    report_rows.append(result)

                    logger.warning(
                        "Invalid row %s: %s",
                        patient["excel_row"],
                        result["message"],
                    )

                    continue

                # ------------------------------------------------
                # DUPLICATE CHECK
                # ------------------------------------------------

                duplicate = check_duplicate(
                    patient,
                    existing_maps,
                    seen_excel,
                )

                if duplicate:

                    status = duplicate["status"]

                    result["status"] = status
                    result["message"] = duplicate["message"]

                    counts[status] += 1

                    issue_rows.append({
                        "excel_row": patient["excel_row"],
                        "patient_name": patient["name"],
                        "mobile": patient["mobile"],
                        "issue": duplicate["message"],
                    })

                    report_rows.append(result)

                    logger.info(
                        "%s | %s | %s | %s",
                        status,
                        patient["name"],
                        patient["mobile"],
                        duplicate["message"],
                    )

                    # Track this row to prevent another Excel
                    # row from bypassing the duplicate check.
                    add_to_excel_seen(
                        patient,
                        seen_excel,
                    )

                    continue

                # ------------------------------------------------
                # MOBILE VALIDATION AFTER NAME MATCHING
                # ------------------------------------------------

                if not patient["normalized_mobile"]:

                    result["status"] = "INVALID"

                    result["message"] = (
                        "Mobile number is missing and "
                        "no matching patient name was found."
                    )

                    counts["INVALID"] += 1

                    issue_rows.append({
                        "excel_row": patient["excel_row"],
                        "patient_name": patient["name"],
                        "mobile": patient["mobile"],
                        "issue": result["message"],
                    })

                    report_rows.append(result)

                    continue

                # ------------------------------------------------
                # DRY RUN
                # ------------------------------------------------

                if dry_run:

                    result["status"] = "WOULD_INSERT"

                    result["message"] = (
                        "No duplicate found in database or "
                        "previous Excel records. "
                        "Patient would be inserted."
                    )

                    counts["WOULD_INSERT"] += 1

                    logger.info(
                        "[DRY RUN] %s | %s",
                        patient["name"],
                        patient["mobile"],
                    )

                    report_rows.append(result)

                    # Track the record so subsequent rows
                    # are checked against it.
                    add_to_excel_seen(
                        patient,
                        seen_excel,
                    )

                    continue

                # ------------------------------------------------
                # NORMAL INSERT
                # ------------------------------------------------

                try:

                    new_patient = insert_patient(
                        cursor,
                        patient,
                    )

                    connection.commit()

                    result["status"] = "INSERTED"

                    result["patient_id"] = (
                        new_patient["patient_id"]
                    )

                    result["message"] = (
                        "Patient inserted successfully."
                    )

                    counts["INSERTED"] += 1

                    # Add successfully inserted patient to
                    # database lookup maps immediately.
                    add_to_lookup_maps(
                        new_patient,
                        existing_maps,
                    )

                    add_to_excel_seen(
                        patient,
                        seen_excel,
                    )

                    logger.info(
                        "INSERTED | %s | %s | %s",
                        new_patient["patient_id"],
                        new_patient["name"],
                        new_patient["mobile"],
                    )

                except Exception as exc:

                    connection.rollback()

                    result["status"] = "FAILED"
                    result["message"] = str(exc)

                    counts["FAILED"] += 1

                    issue_rows.append({
                        "excel_row": patient["excel_row"],
                        "patient_name": patient["name"],
                        "mobile": patient["mobile"],
                        "issue": str(exc),
                    })

                    logger.exception(
                        "Insert failed at Excel row %s",
                        patient["excel_row"],
                    )

                report_rows.append(result)

        if dry_run:
            connection.rollback()

        else:
            connection.commit()

    finally:

        if connection:
            connection.close()

    save_reports(
        report_rows,
        issue_rows,
    )

    print_summary(
        total=len(patients),
        counts=counts,
        mode="DRY RUN" if dry_run else "NORMAL RUN",
    )


# ============================================================
# SUMMARY
# ============================================================

def print_summary(
    total,
    counts,
    mode,
):

    print("\n" + "=" * 65)
    print("PATIENT IMPORT SUMMARY")
    print("=" * 65)

    print(f"Mode                    : {mode}")
    print(f"Total Excel records     : {total}")

    print(
        f"Successfully inserted   : {counts.get('INSERTED', 0)}"
    )

    print(
        f"Would be inserted       : {counts.get('WOULD_INSERT', 0)}"
    )

    print(
        f"Duplicates skipped      : {counts.get('DUPLICATE', 0)}"
    )

    print(
        f"Possible duplicates     : {counts.get('POSSIBLE_DUPLICATE', 0)}"
    )

    print(
        f"Invalid records         : {counts.get('INVALID', 0)}"
    )

    print(
        f"Failed inserts          : {counts.get('FAILED', 0)}"
    )

    print("-" * 65)

    print("Reports:")
    print(f"  {REPORT_FILE}")
    print(f"  {ISSUE_FILE}")
    print(f"  {LOG_FILE}")

    print("=" * 65)

    if mode == "DRY RUN":

        print(
            "\nDRY RUN COMPLETED. "
            "No patient records were inserted."
        )

    elif mode == "PLAN ONLY":

        print(
            "\nPLAN ONLY COMPLETED. "
            "No database changes were made."
        )

    else:

        print(
            "\nPATIENT IMPORT COMPLETED."
        )


# ============================================================
# COMMAND LINE
# ============================================================

def main():

    parser = argparse.ArgumentParser(
        description="Bulk patient import from Excel."
    )

    parser.add_argument(
        "--excel",
        type=str,
        default=str(DEFAULT_EXCEL),
        help="Excel file path.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Check database and Excel duplicates "
            "without inserting patients."
        ),
    )

    parser.add_argument(
        "--plan-only",
        action="store_true",
        help=(
            "Validate Excel and check duplicates "
            "within Excel without database connection."
        ),
    )

    args = parser.parse_args()

    if args.dry_run and args.plan_only:

        parser.error(
            "--dry-run and --plan-only cannot be used together."
        )

    excel_path = Path(args.excel)

    if not excel_path.is_absolute():

        excel_path = SCRIPT_DIR / excel_path

    try:

        run_import(
            excel_path=excel_path,
            dry_run=args.dry_run,
            plan_only=args.plan_only,
        )

    except Exception as exc:

        logger.exception(
            "Patient import stopped: %s",
            exc,
        )

        print(
            f"\nERROR: {exc}"
        )

        sys.exit(1)


if __name__ == "__main__":
    main()
