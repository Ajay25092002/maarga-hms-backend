from datetime import datetime
from decimal import Decimal

import psycopg2
from fastapi import HTTPException
from psycopg2.extras import RealDictCursor

from Core.logger import get_logger
from Schemas.billing_schema import RecordPaymentRequest
from Utils.billing_helpers import (
    ensure_payment_table,
    get_db_connection,
    serialize_row,
)

logger = get_logger(__name__)


def record_payment_service(bill_id: int, payload: RecordPaymentRequest):
    connection = None

    try:
        connection = get_db_connection()

        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:
                ensure_payment_table(cursor)

                # Lock the bill to prevent concurrent overpayments.
                cursor.execute("""
                    SELECT id, total_amount
                    FROM public.bills
                    WHERE id = %s
                    FOR UPDATE
                """, (bill_id,))
                bill = cursor.fetchone()

                if bill is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Bill not found.",
                    )

                cursor.execute("""
                    SELECT COALESCE(SUM(amount), 0) AS amount_paid
                    FROM public.bill_payments
                    WHERE bill_id = %s
                """, (bill_id,))

                already_paid = Decimal(
                    str(cursor.fetchone()["amount_paid"])
                )
                total = Decimal(str(bill["total_amount"]))
                outstanding = max(
                    Decimal("0.00"),
                    total - already_paid,
                )

                if payload.amount <= 0:
                    raise HTTPException(
                        status_code=422,
                        detail="Payment amount must be greater than zero.",
                    )

                if payload.amount > outstanding:
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "message": (
                                "Payment exceeds the outstanding balance."
                            ),
                            "outstanding_amount": float(outstanding),
                        },
                    )

                # receipt_number is intentionally omitted.
                # PostgreSQL generates it using the sequence/default.
                cursor.execute("""
                    INSERT INTO public.bill_payments (
                        bill_id,
                        amount,
                        payment_date,
                        payment_method,
                        reference_number,
                        notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING *
                """, (
                    bill_id,
                    payload.amount,
                    payload.payment_date or datetime.now(),
                    payload.payment_method,
                    payload.reference_number,
                    payload.notes,
                ))

                payment = serialize_row(dict(cursor.fetchone()))

                new_paid = already_paid + payload.amount
                new_outstanding = max(
                    Decimal("0.00"),
                    total - new_paid,
                )

                if total <= 0 or new_paid >= total:
                    payment_status = "Paid"
                elif new_paid > 0:
                    payment_status = "Partially Paid"
                else:
                    payment_status = "Unpaid"

                payment["amount_paid_after_payment"] = float(new_paid)
                payment["outstanding_amount"] = float(new_outstanding)
                payment["payment_status"] = payment_status

                return payment

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception(
            "Database error recording payment for bill %s.",
            bill_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unable to record the payment.",
        )
    except Exception:
        logger.exception(
            "Unexpected error recording payment for bill %s.",
            bill_id,
        )
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while recording the payment.",
        )
    finally:
        if connection is not None:
            connection.close()