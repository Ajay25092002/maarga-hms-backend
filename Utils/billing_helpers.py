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
    """Create or safely migrate the payments table and receipt numbers."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS public.bill_payments (
            id BIGSERIAL PRIMARY KEY,
            bill_id BIGINT NOT NULL
                REFERENCES public.bills(id) ON DELETE RESTRICT,
            amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
            payment_date TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            payment_method VARCHAR(50),
            reference_number VARCHAR(150),
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)

    cursor.execute("""
        ALTER TABLE public.bill_payments
        ADD COLUMN IF NOT EXISTS reference_number VARCHAR(150);
    """)
    cursor.execute("""
        ALTER TABLE public.bill_payments
        ADD COLUMN IF NOT EXISTS notes TEXT;
    """)
    cursor.execute("""
        ALTER TABLE public.bill_payments
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
        NOT NULL DEFAULT NOW();
    """)
    cursor.execute("""
        ALTER TABLE public.bill_payments
        ADD COLUMN IF NOT EXISTS receipt_number VARCHAR(30);
    """)

    cursor.execute("""
        CREATE SEQUENCE IF NOT EXISTS public.bill_payment_receipt_seq
        AS BIGINT
        START WITH 1
        INCREMENT BY 1;
    """)

    cursor.execute("""
        SELECT COUNT(*)
        FROM public.bill_payments
        WHERE receipt_number IS NULL;
    """)
    missing_receipts = cursor.fetchone()["count"]

    if missing_receipts:
        cursor.execute("""
            SELECT COALESCE(
                MAX(
                    CASE
                        WHEN receipt_number ~ '^RC-[0-9]+$'
                        THEN SUBSTRING(receipt_number FROM 4)::BIGINT
                    END
                ),
                0
            )
            FROM public.bill_payments;
        """)
        max_receipt_number = cursor.fetchone()["max"]

        if max_receipt_number > 0:
            cursor.execute(
                """
                SELECT setval(
                    'public.bill_payment_receipt_seq',
                    %s,
                    TRUE
                );
                """,
                (max_receipt_number,),
            )
        else:
            cursor.execute("""
                SELECT setval(
                    'public.bill_payment_receipt_seq',
                    1,
                    FALSE
                );
            """)

        cursor.execute("""
            UPDATE public.bill_payments
            SET receipt_number =
                'RC-' || LPAD(
                    nextval('public.bill_payment_receipt_seq')::TEXT,
                    4,
                    '0'
                )
            WHERE receipt_number IS NULL;
        """)

    cursor.execute("""
        ALTER TABLE public.bill_payments
        ALTER COLUMN receipt_number
        SET DEFAULT (
            'RC-' || LPAD(
                nextval('public.bill_payment_receipt_seq')::TEXT,
                4,
                '0'
            )
        );
    """)

    cursor.execute("""
        ALTER TABLE public.bill_payments
        ALTER COLUMN receipt_number SET NOT NULL;
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
    cursor.execute("""
        SELECT id, bill_id, category, service_date, service_code,
               description, rate, quantity, amount, discount_amount,
               net_amount, created_at
        FROM public.bill_items
        WHERE bill_id = %s
        ORDER BY id
    """, (bill_id,))
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
            status_code=422,
            detail="date_from cannot be later than date_to.",
        )

    start_datetime = (
        datetime.combine(start_date, time.min)
        if start_date else None
    )
    end_datetime = (
        datetime.combine(end_date + timedelta(days=1), time.min)
        if end_date else None
    )

    return start_datetime, end_datetime


BILL_FROM_SQL = """
    FROM public.bills b
    JOIN public.patients p ON p.id = b.patient_db_id
    LEFT JOIN (
        SELECT bill_id, SUM(amount) AS amount_paid
        FROM public.bill_payments
        GROUP BY bill_id
    ) payments ON payments.bill_id = b.id
"""


def build_bill_filters(
    search,
    bill_type,
    payment_status,
    date_filter,
    date_from,
    date_to,
):
    conditions = []
    parameters = []

    if bill_type:
        conditions.append("b.bill_type = %s")
        parameters.append(bill_type)

    if search and search.strip():
        search_value = f"%{search.strip()}%"
        conditions.append("""
            (
                b.bill_number ILIKE %s
                OR p.patient_id ILIKE %s
                OR COALESCE(p.registration_number, '') ILIKE %s
                OR COALESCE(p.name, '') ILIKE %s
                OR COALESCE(b.doctor_name, '') ILIKE %s
                OR COALESCE(p.mobile, '') ILIKE %s
            )
        """)
        parameters.extend([search_value] * 6)

    start_datetime, end_datetime = get_date_range(
        date_filter, date_from, date_to
    )

    if start_datetime:
        conditions.append("b.bill_date >= %s")
        parameters.append(start_datetime)

    if end_datetime:
        conditions.append("b.bill_date < %s")
        parameters.append(end_datetime)

    if payment_status:
        normalized = payment_status.strip().lower()

        if normalized not in {
            "paid", "partially paid", "unpaid"
        }:
            raise HTTPException(
                status_code=422,
                detail="status must be Paid, Partially Paid, or Unpaid.",
            )

        if normalized == "paid":
            conditions.append(
                "COALESCE(payments.amount_paid, 0) >= b.total_amount"
            )
        elif normalized == "partially paid":
            conditions.append("""
                COALESCE(payments.amount_paid, 0) > 0
                AND COALESCE(payments.amount_paid, 0) < b.total_amount
            """)
        else:
            conditions.append(
                "COALESCE(payments.amount_paid, 0) <= 0"
            )

    where_clause = (
        "WHERE " + " AND ".join(conditions)
        if conditions else ""
    )

    return where_clause, parameters