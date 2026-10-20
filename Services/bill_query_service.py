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


# Outstanding is based on original payments. Refunds are tracked separately
# and never reopen a fully-paid bill.
NET_PAID_SQL = "GREATEST(COALESCE(payments.amount_paid, 0) - COALESCE(refunds.total_refunds, 0), 0)"


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

                # Financial summary uses original payments for paid and outstanding totals; refunds are separate.
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
                                        COALESCE(b.total_amount, 0)
                                        - COALESCE(payments.amount_paid, 0),
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
                            COALESCE(b.total_amount, 0) - {NET_PAID_SQL},
                            0
                        ) AS outstanding_amount,

                        CASE WHEN b.bill_type = 'IP' THEN
                            GREATEST(({NET_PAID_SQL}) - b.total_amount, 0)
                        ELSE 0 END AS deposit_balance,

                        CASE
                            WHEN COALESCE(b.total_amount, 0) <= 0
                                THEN 'Paid'
                            WHEN {NET_PAID_SQL} >= COALESCE(b.total_amount, 0)
                                THEN 'Paid'
                            WHEN {NET_PAID_SQL} > 0
                                THEN 'Partially Paid'
                            ELSE 'Unpaid'
                        END AS payment_status,
                        CASE
                            WHEN COALESCE(refunds.total_refunds, 0) <= 0 THEN 'Not Refunded'
                            WHEN COALESCE(refunds.total_refunds, 0) >= COALESCE(payments.amount_paid, 0) THEN 'Fully Refunded'
                            ELSE 'Partially Refunded'
                        END AS refund_status,

                        b.created_at,
                        b.updated_by,
                        b.updated_at,
                        updater.full_name AS updated_by_name
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
                        updater.full_name AS updated_by_name,
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
                            COALESCE(b.total_amount, 0) - {NET_PAID_SQL},
                            0
                        ) AS outstanding_amount,

                        CASE WHEN b.bill_type = 'IP' THEN
                            GREATEST(({NET_PAID_SQL}) - b.total_amount, 0)
                        ELSE 0 END AS deposit_balance,

                        CASE
                            WHEN COALESCE(b.total_amount, 0) <= 0
                                THEN 'Paid'
                            WHEN {NET_PAID_SQL} >= COALESCE(b.total_amount, 0)
                                THEN 'Paid'
                            WHEN {NET_PAID_SQL} > 0
                                THEN 'Partially Paid'
                            ELSE 'Unpaid'
                        END AS payment_status,
                        CASE
                            WHEN COALESCE(refunds.total_refunds, 0) <= 0 THEN 'Not Refunded'
                            WHEN COALESCE(refunds.total_refunds, 0) >= COALESCE(payments.amount_paid, 0) THEN 'Fully Refunded'
                            ELSE 'Partially Refunded'
                        END AS refund_status
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
                result["has_payments"] = len(result["payments"]) > 0

                # Refunds are separate bill-level transactions.
                cursor.execute(
                    """
                    SELECT id, refund_number, bill_id, refund_amount,
                           refund_method, reason, notes, created_by, created_at
                    FROM public.payment_refunds
                    WHERE bill_id = %s
                    ORDER BY created_at DESC, id DESC
                    """,
                    (bill_id,),
                )
                result["refunds"] = [
                    serialize_row(dict(row))
                    for row in cursor.fetchall()
                ]

                # Current financial state is derived from the payment and refund
                # ledger; no duplicate deposit column is stored on bills.
                cursor.execute(
                    """
                    SELECT
                        COALESCE((SELECT SUM(amount) FROM public.bill_payments WHERE bill_id = %s), 0) AS total_payments,
                        COALESCE((SELECT SUM(refund_amount) FROM public.payment_refunds WHERE bill_id = %s), 0) AS total_refunds
                    """,
                    (bill_id, bill_id),
                )
                financials = cursor.fetchone()
                gross_paid = float(financials["total_payments"] or 0)
                refunded = float(financials["total_refunds"] or 0)
                net_paid = max(0.0, gross_paid - refunded)
                total_amount = float(result.get("total_amount") or 0)
                result["total_payments"] = gross_paid
                result["total_refunds"] = refunded
                result["amount_paid"] = net_paid
                result["net_paid"] = net_paid
                result["outstanding_amount"] = max(0.0, total_amount - net_paid)
                result["deposit_balance"] = (
                    max(0.0, net_paid - total_amount)
                    if str(result.get("bill_type", "")).upper() == "IP"
                    else 0.0
                )

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
