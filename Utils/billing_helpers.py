
import os
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional

import psycopg2
from fastapi import HTTPException, status


def get_db_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")

    return psycopg2.connect(database_url)


def ensure_payment_table(cursor):
    """
    Ensure the payment table, receipt sequence, and indexes exist.

    This matches the updated bill_payments schema. It does not attempt
    to migrate legacy tables. Run the database initialization script
    first when recreating the database.
    """
    cursor.execute("""
        CREATE SEQUENCE IF NOT EXISTS public.bill_payment_receipt_seq
        AS BIGINT
        START WITH 1
        INCREMENT BY 1;
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.bill_payments (
            id BIGSERIAL PRIMARY KEY,
            bill_id BIGINT NOT NULL
                REFERENCES public.bills(id) ON DELETE RESTRICT,
            receipt_number VARCHAR(30) NOT NULL
                DEFAULT (
                    'RC-' || LPAD(
                        nextval(
                            'public.bill_payment_receipt_seq'
                        )::TEXT,
                        4,
                        '0'
                    )
                ),
            amount NUMERIC(12, 2) NOT NULL
                CHECK (amount > 0),
            payment_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payment_method VARCHAR(50),
            reference_number VARCHAR(150),
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
    """)

    cursor.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS
            uq_bill_payments_receipt_number
        ON public.bill_payments (receipt_number);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_bill_payments_bill_id
        ON public.bill_payments (bill_id);
    """)

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_bill_payments_payment_date
        ON public.bill_payments (payment_date);
    """)


def generate_bill_number(bill_type: str) -> str:
    """
    Legacy compatibility helper.

    The updated billing service should let the database trigger generate
    bill_number, op_number, and ip_number. Do not use this function in
    the new bill-creation flow.
    """
    if bill_type not in {"OP", "IP"}:
        raise ValueError("bill_type must be OP or IP.")

    prefix = "2MGOPB" if bill_type == "OP" else "2MGIPB"
    bill_date = datetime.now().strftime("%Y%m%d")
    suffix = uuid.uuid4().hex[:8].upper()

    return f"{prefix}-{bill_date}-{suffix}"


def serialize_value(value):
    if isinstance(value, Decimal):
        return float(value)

    if isinstance(value, (datetime, date)):
        return value.isoformat()

    return value


def serialize_row(row: dict) -> dict:
    return {
        key: serialize_value(value)
        for key, value in dict(row).items()
    }


def get_bill_items(cursor, bill_id: int) -> list[dict]:
    """Retrieve bill items using the updated bill_items schema."""
    cursor.execute(
        """
        SELECT
            id,
            bill_id,
            service_id,
            service_group_id,
            service_price_id,
            service_name,
            service_group_name,
            service_date,
            rate,
            quantity,
            amount,
            discount_amount,
            net_amount,
            gst_amount,
            created_at
        FROM public.bill_items
        WHERE bill_id = %s
        ORDER BY id
        """,
        (bill_id,),
    )

    return [
        serialize_row(dict(item))
        for item in cursor.fetchall()
    ]


def get_date_range(
    date_filter: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
):
    today = date.today()
    start_date = date_from
    end_date = date_to

    if date_filter:
        if date_filter == "today":
            start_date = end_date = today

        elif date_filter == "7days":
            start_date = today - timedelta(days=6)
            end_date = today

        elif date_filter == "30days":
            start_date = today - timedelta(days=29)
            end_date = today

        elif date_filter == "this_month":
            start_date = today.replace(day=1)
            end_date = today

        else:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "date_filter must be today, 7days, "
                    "30days, or this_month."
                ),
            )

    if start_date and end_date and start_date > end_date:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be later than date_to.",
        )

    start_datetime = (
        datetime.combine(start_date, time.min)
        if start_date
        else None
    )

    # Exclusive upper bound includes the entire end date.
    end_datetime = (
        datetime.combine(
            end_date + timedelta(days=1),
            time.min,
        )
        if end_date
        else None
    )

    return start_datetime, end_datetime


# Shared bill query joins.
#
# Payment and refund totals are aggregated separately to prevent
# row multiplication when a bill has multiple payments and refunds.
#
# Keep these aliases unique: do not add bill_creator or updater joins
# again in individual service queries that use BILL_FROM_SQL.
BILL_FROM_SQL = """
    FROM public.bills b

    JOIN public.patients p
        ON p.id = b.patient_db_id

    LEFT JOIN public.users AS bill_creator
        ON bill_creator.id = b.created_by

    LEFT JOIN public.users AS updater
        ON updater.id = b.updated_by

    LEFT JOIN (
        SELECT
            bill_id,
            SUM(amount) AS amount_paid
        FROM public.bill_payments
        GROUP BY bill_id
    ) payments
        ON payments.bill_id = b.id

    LEFT JOIN (
        SELECT
            bill_id,
            SUM(refund_amount) AS total_refunds
        FROM public.payment_refunds
        GROUP BY bill_id
    ) refunds
        ON refunds.bill_id = b.id
"""


def build_bill_filters(
    search=None,
    bill_type=None,
    payment_status=None,
    date_filter=None,
    date_from=None,
    date_to=None,
    billed_by=None,
):
    """
    Build SQL filters for bill listing and reporting.

    billed_by filters bills by the user who created them.
    Returns a WHERE clause and its parameter list.
    """
    conditions = []
    parameters = []

    # Filter by the user who created the bill.
    if billed_by is not None:
        conditions.append("b.created_by = %s")
        parameters.append(billed_by)

    # Filter by bill type.
    if bill_type:
        normalized_bill_type = bill_type.strip().upper()

        if normalized_bill_type not in {"OP", "IP"}:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="bill_type must be OP or IP.",
            )

        conditions.append("b.bill_type = %s")
        parameters.append(normalized_bill_type)

    # Search across bill, patient, doctor, and contact details.
    if search and search.strip():
        search_value = f"%{search.strip()}%"

        conditions.append("""
            (
                b.bill_number ILIKE %s
                OR COALESCE(b.op_number, '') ILIKE %s
                OR COALESCE(b.ip_number, '') ILIKE %s
                OR p.patient_id ILIKE %s
                OR COALESCE(p.name, '') ILIKE %s
                OR COALESCE(b.doctor_name, '') ILIKE %s
                OR COALESCE(b.psychologist_name, '') ILIKE %s
                OR COALESCE(p.mobile, '') ILIKE %s
            )
        """)

        parameters.extend([search_value] * 8)

    # Filter by date range.
    start_datetime, end_datetime = get_date_range(
        date_filter,
        date_from,
        date_to,
    )

    if start_datetime:
        conditions.append("b.bill_date >= %s")
        parameters.append(start_datetime)

    if end_datetime:
        conditions.append("b.bill_date < %s")
        parameters.append(end_datetime)

    # Filter payment status from original payments; refunds do not reopen bills.
    if payment_status:
        normalized = payment_status.strip().lower()

        if normalized not in {
            "paid",
            "partially paid",
            "unpaid",
        }:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "status must be Paid, Partially Paid, or Unpaid."
                ),
            )

        paid_amount = "COALESCE(payments.amount_paid, 0)"

        if normalized == "paid":
            conditions.append(
                f"({paid_amount}) >= b.total_amount"
            )

        elif normalized == "partially paid":
            conditions.append(f"""
                ({paid_amount}) > 0
                AND ({paid_amount}) < b.total_amount
            """)

        else:
            conditions.append(
                f"({paid_amount}) <= 0"
            )

    where_clause = (
        "WHERE " + " AND ".join(conditions)
        if conditions
        else ""
    )

    return where_clause, parameters
