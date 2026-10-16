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


# Keep this expression consistent across list, summary, and detail queries.
# Original payments are retained; refunds are deducted only when calculating
# net paid and outstanding amounts.
NET_PAID_SQL = """
    GREATEST(
        COALESCE(payments.amount_paid, 0)
        - COALESCE(refunds.total_refunds, 0),
        0
    )
"""


def list_bills_service(
    page,
    page_size,
    search,
    bill_type,
    status_filter,
    date_filter,
    date_from,
    date_to,
    billed_by=None,
):
    connection = None

    try:
        connection = get_db_connection()

        with connection:
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
                    billed_by=billed_by,
                )

                # Count matching bills. BILL_FROM_SQL joins only aggregate
                # subqueries, so each bill remains one row.
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS total
                    {BILL_FROM_SQL}
                    {where_clause}
                    """,
                    tuple(parameters),
                )
                total = cursor.fetchone()["total"]

                # Financial summary uses net paid after refunds.
                cursor.execute(
                    f"""
                    SELECT
                        COALESCE(
                            SUM(b.total_amount),
                            0
                        ) AS billed,

                        COALESCE(
                            SUM(
                                LEAST(
                                    {NET_PAID_SQL},
                                    GREATEST(b.total_amount, 0)
                                )
                            ),
                            0
                        ) AS received,

                        COALESCE(
                            SUM(
                                GREATEST(
                                    b.total_amount - ({NET_PAID_SQL}),
                                    0
                                )
                            ),
                            0
                        ) AS outstanding,

                        COALESCE(
                            SUM(COALESCE(refunds.total_refunds, 0)),
                            0
                        ) AS refunded
                    {BILL_FROM_SQL}
                    {where_clause}
                    """,
                    tuple(parameters),
                )
                summary = serialize_row(dict(cursor.fetchone()))

                offset = (page - 1) * page_size

                # Paginated bills with payment/refund-aware financial values.
                cursor.execute(
                    f"""
                    SELECT
                        b.id,
                        b.bill_number,
                        b.bill_type,
                        b.created_by,
                        bill_creator.full_name AS created_by_name,
                        b.op_number,
                        b.ip_number,

                        b.patient_db_id,
                        p.patient_id,
                        p.name AS patient_name,
                        p.mobile,
                        p.address,

                        b.bill_date,
                        b.department,

                        b.room_type_id,
                        b.room_type_name,

                        b.doctor_id,
                        b.doctor_name,

                        b.psychologist_id,
                        b.psychologist_name,

                        b.admission_date,
                        b.discharge_date,
                        b.bed_number,
                        b.rate_category,

                        b.subtotal,
                        b.discount_amount,
                        b.total_amount,

                        COALESCE(payments.amount_paid, 0)
                            AS total_payments,

                        COALESCE(refunds.total_refunds, 0)
                            AS total_refunds,

                        {NET_PAID_SQL} AS amount_paid,

                        GREATEST(
                            b.total_amount - ({NET_PAID_SQL}),
                            0
                        ) AS outstanding_amount,

                        CASE
                            WHEN b.total_amount <= 0
                                THEN 'Paid'
                            WHEN ({NET_PAID_SQL}) >= b.total_amount
                                THEN 'Paid'
                            WHEN ({NET_PAID_SQL}) > 0
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
                            (total + page_size - 1) // page_size
                            if page_size > 0
                            else 0
                        ),
                    },
                    "summary": summary,
                }

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Database error while listing bills.")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve bills.",
        )

    except Exception:
        logger.exception("Unexpected error while listing bills.")
        raise HTTPException(
            status_code=500,
            detail=(
                "An unexpected error occurred while retrieving bills."
            ),
        )

    finally:
        if connection is not None:
            connection.close()


def list_bill_creators_service():
    """Return users who have created at least one bill for the creator filter."""
    connection = None
    try:
        connection = get_db_connection()
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    SELECT DISTINCT u.id, u.full_name
                    FROM public.users u
                    JOIN public.bills b ON b.created_by = u.id
                    WHERE u.full_name IS NOT NULL
                    ORDER BY u.full_name ASC, u.id ASC
                """)
                return [
                    {"id": row["id"], "full_name": row["full_name"]}
                    for row in cursor.fetchall()
                ]
    except psycopg2.Error:
        logger.exception("Database error listing bill creators.")
        raise HTTPException(status_code=500, detail="Unable to retrieve bill creators.")
    finally:
        if connection is not None:
            connection.close()


def get_bill_service(bill_id: int):
    connection = None

    try:
        connection = get_db_connection()

        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:
                ensure_payment_table(cursor)

                # Retrieve bill details and payment/refund-aware totals.
                cursor.execute(
                    f"""
                    SELECT
                        b.*,
                        bill_creator.full_name AS created_by_name,

                        p.patient_id,
                        p.name AS patient_name,
                        p.mobile,
                        p.email,
                        p.address,
                        p.gender,
                        p.date_of_birth,

                        COALESCE(payments.amount_paid, 0)
                            AS total_payments,

                        COALESCE(refunds.total_refunds, 0)
                            AS total_refunds,

                        {NET_PAID_SQL} AS amount_paid,

                        GREATEST(
                            b.total_amount - ({NET_PAID_SQL}),
                            0
                        ) AS outstanding_amount,

                        CASE
                            WHEN b.total_amount <= 0
                                THEN 'Paid'
                            WHEN ({NET_PAID_SQL}) >= b.total_amount
                                THEN 'Paid'
                            WHEN ({NET_PAID_SQL}) > 0
                                THEN 'Partially Paid'
                            ELSE 'Unpaid'
                        END AS payment_status
                    {BILL_FROM_SQL}
                    WHERE b.id = %s
                    """,
                    (bill_id,),
                )

                bill = cursor.fetchone()

                if bill is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Bill not found.",
                    )

                result = serialize_row(dict(bill))

                # Bill items and original payment records remain unchanged.
                result["items"] = get_bill_items(cursor, bill_id)

                cursor.execute(
                    """
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
                    """,
                    (bill_id,),
                )
                result["payments"] = [
                    serialize_row(dict(row))
                    for row in cursor.fetchall()
                ]

                # Refunds are separate records linked to the original payment.
                cursor.execute(
                    """
                    SELECT
                        pr.id,
                        pr.refund_number,
                        pr.payment_id,
                        bp.receipt_number,
                        bp.bill_id,
                        pr.refund_amount,
                        pr.refund_method,
                        pr.reason,
                        pr.notes,
                        pr.created_by,
                        pr.created_at
                    FROM public.payment_refunds pr
                    JOIN public.bill_payments bp
                        ON bp.id = pr.payment_id
                    WHERE bp.bill_id = %s
                    ORDER BY pr.created_at DESC, pr.id DESC
                    """,
                    (bill_id,),
                )
                result["refunds"] = [
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
            status_code=500,
            detail="Unable to retrieve the bill.",
        )

    except Exception:
        logger.exception(
            "Unexpected error retrieving bill %s.",
            bill_id,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "An unexpected error occurred while retrieving the bill."
            ),
        )

    finally:
        if connection is not None:
            connection.close()
