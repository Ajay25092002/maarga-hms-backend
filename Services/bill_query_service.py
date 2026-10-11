import psycopg2
from fastapi import HTTPException
from psycopg2.extras import RealDictCursor

from Core.logger import get_logger
from Utils.billing_helpers import (
    BILL_FROM_SQL,
    build_bill_filters,
    ensure_payment_table,
    get_bill_items,
    get_db_connection,
    serialize_row,
)

logger = get_logger(__name__)


def list_bills_service(
    page,
    page_size,
    search,
    bill_type,
    status_filter,
    date_filter,
    date_from,
    date_to,
):
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            ensure_payment_table(cursor)

            where_clause, parameters = build_bill_filters(
                search,
                bill_type,
                status_filter,
                date_filter,
                date_from,
                date_to,
            )

            cursor.execute(
                f"""
                SELECT COUNT(*) AS total
                {BILL_FROM_SQL}
                {where_clause}
                """,
                tuple(parameters),
            )
            total = cursor.fetchone()["total"]

            cursor.execute(
                f"""
                SELECT
                    COALESCE(SUM(b.total_amount), 0) AS billed,
                    COALESCE(
                        SUM(LEAST(
                            COALESCE(payments.amount_paid, 0),
                            b.total_amount
                        )),
                        0
                    ) AS received,
                    COALESCE(
                        SUM(GREATEST(
                            b.total_amount
                            - COALESCE(payments.amount_paid, 0),
                            0
                        )),
                        0
                    ) AS outstanding
                {BILL_FROM_SQL}
                {where_clause}
                """,
                tuple(parameters),
            )
            summary = serialize_row(dict(cursor.fetchone()))

            offset = (page - 1) * page_size

            cursor.execute(
                f"""
                SELECT
                    b.id,
                    b.bill_number,
                    b.bill_type,
                    b.patient_db_id,
                    b.registration_number,
                    p.patient_id,
                    p.name AS patient_name,
                    p.mobile,
                    p.address,
                    b.bill_date,
                    b.department,
                    b.doctor_name,
                    b.subtotal,
                    b.discount_amount,
                    b.total_amount,
                    COALESCE(payments.amount_paid, 0) AS amount_paid,
                    GREATEST(
                        b.total_amount
                        - COALESCE(payments.amount_paid, 0),
                        0
                    ) AS outstanding_amount,
                    CASE
                        WHEN b.total_amount <= 0
                          OR COALESCE(payments.amount_paid, 0)
                             >= b.total_amount
                            THEN 'Paid'
                        WHEN COALESCE(payments.amount_paid, 0) > 0
                            THEN 'Partially Paid'
                        ELSE 'Unpaid'
                    END AS payment_status,
                    b.created_at
                {BILL_FROM_SQL}
                {where_clause}
                ORDER BY b.bill_date DESC, b.id DESC
                LIMIT %s OFFSET %s
                """,
                tuple(parameters + [page_size, offset]),
            )

            bills = [
                serialize_row(dict(row))
                for row in cursor.fetchall()
            ]

            return {
                "items": bills,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": (
                        total + page_size - 1
                    ) // page_size,
                },
                "summary": summary,
            }

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception("Database error while listing bills.")
        raise HTTPException(500, "Unable to retrieve bills.")
    except Exception:
        logger.exception("Unexpected error while listing bills.")
        raise HTTPException(
            500,
            "An unexpected error occurred while retrieving bills.",
        )
    finally:
        if connection is not None:
            connection.close()


def get_bill_service(bill_id: int):
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            ensure_payment_table(cursor)

            cursor.execute("""
                SELECT
                    b.*,
                    p.patient_id,
                    p.name AS patient_name,
                    p.mobile,
                    p.email,
                    p.address,
                    p.gender,
                    p.date_of_birth,
                    p.registration_number
                        AS patient_registration_number,
                    COALESCE(payments.amount_paid, 0) AS amount_paid,
                    GREATEST(
                        b.total_amount
                        - COALESCE(payments.amount_paid, 0),
                        0
                    ) AS outstanding_amount,
                    CASE
                        WHEN b.total_amount <= 0
                          OR COALESCE(payments.amount_paid, 0)
                             >= b.total_amount
                            THEN 'Paid'
                        WHEN COALESCE(payments.amount_paid, 0) > 0
                            THEN 'Partially Paid'
                        ELSE 'Unpaid'
                    END AS payment_status
                FROM public.bills b
                JOIN public.patients p
                    ON p.id = b.patient_db_id
                LEFT JOIN (
                    SELECT bill_id, SUM(amount) AS amount_paid
                    FROM public.bill_payments
                    GROUP BY bill_id
                ) payments ON payments.bill_id = b.id
                WHERE b.id = %s
            """, (bill_id,))

            bill = cursor.fetchone()

            if bill is None:
                raise HTTPException(
                    status_code=404,
                    detail="Bill not found.",
                )

            result = serialize_row(dict(bill))
            result["items"] = get_bill_items(cursor, bill_id)

            cursor.execute("""
                SELECT
                    id,
                    receipt_number,
                    bill_id,
                    amount,
                    payment_date,
                    payment_method,
                    reference_number,
                    notes,
                    created_at
                FROM public.bill_payments
                WHERE bill_id = %s
                ORDER BY payment_date DESC, id DESC
            """, (bill_id,))

            result["payments"] = [
                serialize_row(dict(row))
                for row in cursor.fetchall()
            ]

            return result

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception(
            "Database error retrieving bill %s.",
            bill_id,
        )
        raise HTTPException(
            500,
            "Unable to retrieve the bill.",
        )
    except Exception:
        logger.exception(
            "Unexpected error retrieving bill %s.",
            bill_id,
        )
        raise HTTPException(
            500,
            "An unexpected error occurred while retrieving the bill.",
        )
    finally:
        if connection is not None:
            connection.close()