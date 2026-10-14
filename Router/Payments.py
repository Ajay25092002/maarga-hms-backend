
from datetime import date
from typing import Optional

import psycopg2
from fastapi import APIRouter, HTTPException, Path, Query, status
from psycopg2.extras import RealDictCursor

from Schemas.billing_schema import RecordPaymentRequest
from Services.payment_pdf_service import generate_payment_receipt_pdf
from Services.payment_service import record_payment_service
from Utils.billing_helpers import get_db_connection, serialize_row


router = APIRouter(
    prefix="/api/payments",
    tags=["Payments and Receipts"],
)


@router.get("/{payment_id}/receipt-pdf")
def get_payment_receipt_pdf(
    payment_id: int = Path(gt=0),
):
    """Return a printable PDF for a recorded payment."""
    return generate_payment_receipt_pdf(payment_id)


@router.get("")
def list_payments(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    search: Optional[str] = Query(default=None, max_length=100),
    payment_method: Optional[str] = Query(default=None, max_length=50),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
    bill_type: Optional[str] = Query(
        default=None,
        pattern="^(OP|IP)$",
    ),
):
    """List recorded payments. This endpoint is read-only."""

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from cannot be later than date_to.",
        )

    connection = None

    try:
        connection = get_db_connection()

        where = []
        params = []

        if search and search.strip():
            where.append(
                """
                (
                    bp.receipt_number ILIKE %s
                    OR p.patient_id ILIKE %s
                    OR p.name ILIKE %s
                    OR b.bill_number ILIKE %s
                    OR COALESCE(b.op_number, '') ILIKE %s
                    OR COALESCE(b.ip_number, '') ILIKE %s
                    OR COALESCE(bp.reference_number, '') ILIKE %s
                    OR CAST(bp.id AS TEXT) ILIKE %s
                )
                """
            )

            term = f"%{search.strip()}%"
            params.extend([term] * 8)

        if payment_method:
            where.append("bp.payment_method = %s")
            params.append(payment_method)

        if date_from:
            where.append("bp.payment_date >= %s")
            params.append(date_from)

        if date_to:
            where.append(
                "bp.payment_date < (%s::date + INTERVAL '1 day')"
            )
            params.append(date_to)

        if bill_type:
            where.append("b.bill_type = %s")
            params.append(bill_type)

        where_sql = (
            "WHERE " + " AND ".join(where)
            if where
            else ""
        )

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            # Total matching payment records
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM public.bill_payments bp
                JOIN public.bills b
                    ON b.id = bp.bill_id
                JOIN public.patients p
                    ON p.id = b.patient_db_id
                {where_sql}
                """,
                tuple(params),
            )

            total = cursor.fetchone()["total"]

            offset = (page - 1) * page_size

            # Paginated payment records with bill balances
            cursor.execute(
                f"""
                SELECT
                    bp.id,
                    bp.receipt_number,
                    bp.bill_id,
                    bp.amount,
                    bp.payment_date,
                    bp.payment_method,
                    bp.reference_number,
                    bp.notes,
                    bp.created_at,

                    b.bill_number,
                    b.bill_type,
                    b.op_number,
                    b.ip_number,
                    b.total_amount AS bill_total,

                    p.patient_id,
                    p.name AS patient_name,
                    p.mobile AS patient_mobile,

                    COALESCE((
                        SELECT SUM(bp2.amount)
                        FROM public.bill_payments bp2
                        WHERE bp2.bill_id = b.id
                    ), 0) AS bill_amount_paid,

                    GREATEST(
                        b.total_amount - COALESCE((
                            SELECT SUM(bp3.amount)
                            FROM public.bill_payments bp3
                            WHERE bp3.bill_id = b.id
                        ), 0),
                        0
                    ) AS bill_outstanding_amount

                FROM public.bill_payments bp
                JOIN public.bills b
                    ON b.id = bp.bill_id
                JOIN public.patients p
                    ON p.id = b.patient_db_id

                {where_sql}

                ORDER BY bp.payment_date DESC, bp.id DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [page_size, offset]),
            )

            rows = [
                serialize_row(dict(row))
                for row in cursor.fetchall()
            ]

        return {
            "items": rows,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (
                    total + page_size - 1
                ) // page_size,
            },
        }

    except HTTPException:
        raise

    except psycopg2.Error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve payments.",
        )

    finally:
        if connection is not None:
            connection.close()


@router.post(
    "/bills/{bill_id}",
    status_code=status.HTTP_201_CREATED,
)
def create_payment(
    payload: RecordPaymentRequest,
    bill_id: int = Path(gt=0),
):
    """
    Record a payment against an existing bill.
    Payment editing is intentionally not exposed.
    """
    return record_payment_service(bill_id, payload)


@router.get("/{payment_id}")
def get_payment(
    payment_id: int = Path(gt=0),
):
    """Get one payment with its bill and patient details."""

    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

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
                    bp.created_at,

                    b.bill_number,
                    b.bill_type,
                    b.op_number,
                    b.ip_number,
                    b.total_amount AS bill_total,

                    p.patient_id,
                    p.name AS patient_name,
                    p.mobile AS patient_mobile,

                    COALESCE((
                        SELECT SUM(bp2.amount)
                        FROM public.bill_payments bp2
                        WHERE bp2.bill_id = b.id
                    ), 0) AS bill_amount_paid,

                    GREATEST(
                        b.total_amount - COALESCE((
                            SELECT SUM(bp3.amount)
                            FROM public.bill_payments bp3
                            WHERE bp3.bill_id = b.id
                        ), 0),
                        0
                    ) AS bill_outstanding_amount

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
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Payment not found.",
                )

            return serialize_row(dict(row))

    except HTTPException:
        raise

    except psycopg2.Error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve payment.",
        )

    finally:
        if connection is not None:
            connection.close()
