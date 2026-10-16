
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from psycopg2.extras import RealDictCursor
from Router.Login import get_current_user

from Utils.billing_helpers import get_db_connection, serialize_row


router = APIRouter(
    prefix="/api/payment-refunds",
    tags=["Payment Refunds"],
)


# -------------------------------------------------------------------
# Request schema
# -------------------------------------------------------------------

class CreatePaymentRefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: int = Field(gt=0)
    refund_amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    refund_method: str = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=2000)
    notes: Optional[str] = Field(default=None, max_length=5000)

    


# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def _clean_required_text(value: str, field_name: str) -> str:
    cleaned = value.strip()

    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"{field_name} cannot be empty.",
        )

    return cleaned


def _build_filters(
    search: Optional[str] = None,
    payment_id: Optional[int] = None,
    refund_method: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
):
    """
    Build reusable WHERE conditions for the refund list and KPI APIs.

    Search covers refund number, original payment receipt/reference,
    refund method, reason, notes, and the user who recorded the refund.
    """
    conditions = ["1 = 1"]
    params = []

    if search and search.strip():
        search_term = f"%{search.strip()}%"
        conditions.append(
            """
            (
                pr.refund_number ILIKE %s
                OR bp.receipt_number ILIKE %s
                OR COALESCE(bp.reference_number, '') ILIKE %s
                OR pr.refund_method ILIKE %s
                OR pr.reason ILIKE %s
                OR COALESCE(pr.notes, '') ILIKE %s
                OR COALESCE(u.full_name, '') ILIKE %s
            )
            """
        )
        params.extend([search_term] * 7)

    if payment_id is not None:
        conditions.append("pr.payment_id = %s")
        params.append(payment_id)

    if refund_method:
        conditions.append("LOWER(pr.refund_method) = LOWER(%s)")
        params.append(refund_method.strip())

    if date_from:
        conditions.append("pr.created_at >= %s")
        params.append(datetime.combine(date_from, time.min))

    if date_to:
        # Exclusive upper bound includes the entire date_to day.
        conditions.append("pr.created_at < %s")
        params.append(
            datetime.combine(date_to + timedelta(days=1), time.min)
        )

    return " AND ".join(conditions), params


def _validate_date_range(
    date_from: Optional[date],
    date_to: Optional[date],
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="date_from must be on or before date_to.",
        )


def _get_payment_balance(cursor, payment_id: int):
    """
    Return original payment amount, amount refunded, and remaining balance.
    The caller must ensure the payment exists.
    """
    cursor.execute(
        """
        SELECT
            bp.id AS payment_id,
            bp.bill_id,
            bp.receipt_number,
            bp.amount AS original_payment_amount,
            COALESCE(SUM(pr.refund_amount), 0) AS total_refunded,
            GREATEST(
                bp.amount - COALESCE(SUM(pr.refund_amount), 0),
                0
            ) AS refundable_balance
        FROM public.bill_payments bp
        LEFT JOIN public.payment_refunds pr
            ON pr.payment_id = bp.id
        WHERE bp.id = %s
        GROUP BY
            bp.id,
            bp.bill_id,
            bp.receipt_number,
            bp.amount
        """,
        (payment_id,),
    )

    return cursor.fetchone()


def _close_connection(connection):
    if connection is not None:
        connection.close()


# -------------------------------------------------------------------
# 1. Create a refund
# POST /api/payment-refunds
# Stores the refund and returns recalculated bill-level totals. The bills row
# and original payment rows are intentionally not overwritten; totals are
# derived from bill_payments minus payment_refunds.
# -------------------------------------------------------------------

@router.post("", status_code=status.HTTP_201_CREATED)
def create_payment_refund(
    payload: CreatePaymentRefundRequest,
    current_user: dict = Depends(get_current_user),
):
    refund_method = _clean_required_text(
        payload.refund_method,
        "refund_method",
    )
    reason = _clean_required_text(payload.reason, "reason")
    notes = payload.notes.strip() if payload.notes else None

    if notes == "":
        notes = None

    if payload.refund_amount <= Decimal("0.00"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Refund amount must be greater than zero.",
        )

    connection = None

    try:
        connection = get_db_connection()

        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:
                # Lock the payment row to serialize refund attempts for
                # the same payment and prevent concurrent over-refunds.
                cursor.execute(
                    """
                    SELECT
                        id,
                        bill_id,
                        receipt_number,
                        amount
                    FROM public.bill_payments
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (payload.payment_id,),
                )
                payment = cursor.fetchone()

                if not payment:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Payment not found.",
                    )

                cursor.execute(
                    """
                    SELECT COALESCE(SUM(refund_amount), 0)
                        AS total_refunded
                    FROM public.payment_refunds
                    WHERE payment_id = %s
                    """,
                    (payload.payment_id,),
                )
                refund_totals = cursor.fetchone()

                original_amount = Decimal(str(payment["amount"]))
                total_refunded = Decimal(
                    str(refund_totals["total_refunded"])
                )
                refundable_balance = original_amount - total_refunded

                if payload.refund_amount > refundable_balance:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail={
                            "message": (
                                "Refund amount exceeds the remaining "
                                "refundable balance."
                            ),
                            "original_payment_amount": str(original_amount),
                            "total_refunded": str(total_refunded),
                            "refundable_balance": str(
                                max(refundable_balance, Decimal("0.00"))
                            ),
                        },
                    )

                # Use the authenticated user's ID; never trust a user ID
                # supplied by the client for audit fields.
                try:
                    created_by = int(current_user["user"]["id"])
                except (KeyError, TypeError, ValueError):
                    raise HTTPException(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        detail="Unable to identify the authenticated user.",
                    ) from None

                cursor.execute(
                    """
                    SELECT id
                    FROM public.users
                    WHERE id = %s
                    """,
                    (created_by,),
                )
                user = cursor.fetchone()

                if not user:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="The specified user does not exist.",
                    )

                cursor.execute(
                    """
                    INSERT INTO public.payment_refunds (
                        payment_id,
                        refund_amount,
                        refund_method,
                        reason,
                        notes,
                        created_by
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING
                        id,
                        refund_number,
                        payment_id,
                        refund_amount,
                        refund_method,
                        reason,
                        notes,
                        created_by,
                        created_at
                    """,
                    (
                        payload.payment_id,
                        payload.refund_amount,
                        refund_method,
                        reason,
                        notes,
                        created_by,
                    ),
                )
                refund = dict(cursor.fetchone())

                updated_balance = (
                    refundable_balance - payload.refund_amount
                )

                # Recalculate bill-level payment totals from source records.
                # Aggregate payments and refunds separately to avoid join multiplication.
                cursor.execute(
                    """
                    SELECT
                        b.id AS bill_id,
                        b.total_amount AS bill_total,
                        COALESCE(payments.total_payments, 0) AS total_paid,
                        COALESCE(refunds.total_refunded, 0) AS total_refunded
                    FROM public.bills b
                    LEFT JOIN (
                        SELECT bill_id, SUM(amount) AS total_payments
                        FROM public.bill_payments
                        GROUP BY bill_id
                    ) payments ON payments.bill_id = b.id
                    LEFT JOIN (
                        SELECT bp.bill_id, SUM(pr.refund_amount) AS total_refunded
                        FROM public.payment_refunds pr
                        JOIN public.bill_payments bp ON bp.id = pr.payment_id
                        GROUP BY bp.bill_id
                    ) refunds ON refunds.bill_id = b.id
                    WHERE b.id = %s
                    """,
                    (payment["bill_id"],),
                )
                bill_summary = cursor.fetchone()

                if not bill_summary:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="Bill linked to this payment was not found.",
                    )

                bill_total = Decimal(str(bill_summary["bill_total"] or 0))
                total_paid = Decimal(str(bill_summary["total_paid"] or 0))
                bill_refunded = Decimal(str(bill_summary["total_refunded"] or 0))
                net_paid = max(total_paid - bill_refunded, Decimal("0.00"))
                outstanding_amount = max(bill_total - net_paid, Decimal("0.00"))
                payment_status = (
                    "Paid" if outstanding_amount == 0
                    else "Unpaid" if net_paid == 0
                    else "Partially Paid"
                )

                response = {
                    **serialize_row(refund),
                    "bill_id": payment["bill_id"],
                    "receipt_number": payment["receipt_number"],
                    "original_payment_amount": original_amount,
                    "total_refunded_for_payment": (
                        total_refunded + payload.refund_amount
                    ),
                    "refundable_balance": updated_balance,
                    "bill_total": bill_total,
                    "total_paid": total_paid,
                    "total_refunded_for_bill": bill_refunded,
                    "net_paid": net_paid,
                    "outstanding_amount": outstanding_amount,
                    "payment_status": payment_status,
                }

        return serialize_row(response)

    except HTTPException:
        raise
    except psycopg2.Error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while recording the refund.",
        )
    finally:
        _close_connection(connection)


# -------------------------------------------------------------------
# 2. List refunds with pagination, search, and filters
# GET /api/payment-refunds
# -------------------------------------------------------------------

@router.get("")
def list_payment_refunds(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    search: Optional[str] = Query(default=None, max_length=150),
    payment_id: Optional[int] = Query(default=None, gt=0),
    refund_method: Optional[str] = Query(default=None, max_length=50),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
):
    _validate_date_range(date_from, date_to)

    if refund_method is not None and not refund_method.strip():
        refund_method = None

    where_clause, filter_params = _build_filters(
        search=search,
        payment_id=payment_id,
        refund_method=refund_method,
        date_from=date_from,
        date_to=date_to,
    )

    offset = (page - 1) * page_size
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total_records
                FROM public.payment_refunds pr
                JOIN public.bill_payments bp
                    ON bp.id = pr.payment_id
                JOIN public.users u
                    ON u.id = pr.created_by
                WHERE {where_clause}
                """,
                filter_params,
            )
            total_records = cursor.fetchone()["total_records"]

            cursor.execute(
                f"""
                SELECT
                    pr.id,
                    pr.refund_number,
                    pr.payment_id,
                    pr.refund_amount,
                    pr.refund_method,
                    pr.reason,
                    pr.notes,
                    pr.created_by,
                    u.full_name AS created_by_name,
                    pr.created_at,
                    bp.bill_id,
                    bp.receipt_number,
                    bp.amount AS original_payment_amount,
                    bp.payment_method AS original_payment_method,
                    bp.payment_date,
                    COALESCE(refund_totals.total_refunded, 0)
                        AS total_refunded_for_payment,
                    GREATEST(
                        bp.amount
                        - COALESCE(refund_totals.total_refunded, 0),
                        0
                    ) AS refundable_balance
                FROM public.payment_refunds pr
                JOIN public.bill_payments bp
                    ON bp.id = pr.payment_id
                JOIN public.bills b
                    ON b.id = bp.bill_id
                JOIN public.patients p
                    ON p.id = b.patient_db_id
                JOIN public.users u
                    ON u.id = pr.created_by
                LEFT JOIN (
                    SELECT
                        payment_id,
                        SUM(refund_amount) AS total_refunded
                    FROM public.payment_refunds
                    GROUP BY payment_id
                ) refund_totals
                    ON refund_totals.payment_id = bp.id
                WHERE {where_clause}
                ORDER BY pr.created_at DESC, pr.id DESC
                LIMIT %s OFFSET %s
                """,
                filter_params + [page_size, offset],
            )
            items = [
                serialize_row(row)
                for row in cursor.fetchall()
            ]

        total_pages = (
            (total_records + page_size - 1) // page_size
            if total_records
            else 0
        )

        return {
            "items": items,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total_records": total_records,
                "total_pages": total_pages,
                "has_next": page < total_pages,
                "has_previous": page > 1 and total_pages > 0,
            },
            "filters": {
                "search": search,
                "payment_id": payment_id,
                "refund_method": refund_method,
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
            },
        }

    except psycopg2.Error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while retrieving refunds.",
        )
    finally:
        _close_connection(connection)


# -------------------------------------------------------------------
# 3. Refund KPI summary
# GET /api/payment-refunds/summary
# -------------------------------------------------------------------

@router.get("/summary")
def get_payment_refund_summary(
    search: Optional[str] = Query(default=None, max_length=150),
    payment_id: Optional[int] = Query(default=None, gt=0),
    refund_method: Optional[str] = Query(default=None, max_length=50),
    date_from: Optional[date] = Query(default=None),
    date_to: Optional[date] = Query(default=None),
):
    """
    KPIs use every record matching the filters, not just the current page.
    """
    _validate_date_range(date_from, date_to)

    if refund_method is not None and not refund_method.strip():
        refund_method = None

    where_clause, filter_params = _build_filters(
        search=search,
        payment_id=payment_id,
        refund_method=refund_method,
        date_from=date_from,
        date_to=date_to,
    )

    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) AS refund_count,
                    COALESCE(SUM(pr.refund_amount), 0)
                        AS total_refunded_amount,
                    COALESCE(AVG(pr.refund_amount), 0)
                        AS average_refund_amount,
                    COUNT(DISTINCT pr.payment_id)
                        AS payments_with_refunds,
                    MIN(pr.created_at) AS first_refund_at,
                    MAX(pr.created_at) AS latest_refund_at
                FROM public.payment_refunds pr
                JOIN public.bill_payments bp
                    ON bp.id = pr.payment_id
                JOIN public.users u
                    ON u.id = pr.created_by
                WHERE {where_clause}
                """,
                filter_params,
            )

            summary = serialize_row(cursor.fetchone())

            cursor.execute(
                f"""
                SELECT
                    pr.refund_method,
                    COUNT(*) AS refund_count,
                    COALESCE(SUM(pr.refund_amount), 0)
                        AS total_refunded_amount
                FROM public.payment_refunds pr
                JOIN public.bill_payments bp
                    ON bp.id = pr.payment_id
                JOIN public.users u
                    ON u.id = pr.created_by
                WHERE {where_clause}
                GROUP BY pr.refund_method
                ORDER BY total_refunded_amount DESC
                """,
                filter_params,
            )

            by_method = [
                serialize_row(row)
                for row in cursor.fetchall()
            ]

        return {
            **summary,
            "by_refund_method": by_method,
            "filters": {
                "search": search,
                "payment_id": payment_id,
                "refund_method": refund_method,
                "date_from": date_from.isoformat() if date_from else None,
                "date_to": date_to.isoformat() if date_to else None,
            },
        }

    except psycopg2.Error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while retrieving refund summary.",
        )
    finally:
        _close_connection(connection)


# -------------------------------------------------------------------
# 4. Refundable balance for an original payment
# GET /api/payment-refunds/payments/{payment_id}/balance
# -------------------------------------------------------------------

@router.get("/payments/{payment_id}/balance")
def get_payment_refund_balance(
    payment_id: int = Path(gt=0),
):
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            balance = _get_payment_balance(cursor, payment_id)

            if not balance:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Payment not found.",
                )

        return serialize_row(balance)

    except HTTPException:
        raise
    except psycopg2.Error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while retrieving payment balance.",
        )
    finally:
        _close_connection(connection)


# -------------------------------------------------------------------
# 5. Refund details
# GET /api/payment-refunds/{refund_id}
# -------------------------------------------------------------------

@router.get("/{refund_id}")
def get_payment_refund_details(
    refund_id: int = Path(gt=0),
):
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                """
                SELECT
                    pr.id,
                    pr.refund_number,
                    pr.payment_id,
                    pr.refund_amount,
                    pr.refund_method,
                    pr.reason,
                    pr.notes,
                    pr.created_by,
                    u.full_name AS created_by_name,
                    pr.created_at,
                    bp.bill_id,
                    bp.receipt_number,
                    bp.amount AS original_payment_amount,
                    bp.payment_method AS original_payment_method,
                    bp.reference_number AS original_payment_reference,
                    bp.payment_date,
                    b.bill_number,
                    p.name AS patient_name,
                    p.patient_id AS patient_uhid,
                    p.mobile AS patient_mobile,
                    GREATEST(
                        bp.amount - COALESCE(refund_totals.total_refunded, 0),
                        0
                    ) AS refundable_balance
                FROM public.payment_refunds pr
                JOIN public.bill_payments bp
                    ON bp.id = pr.payment_id
                JOIN public.bills b
                    ON b.id = bp.bill_id
                JOIN public.patients p
                    ON p.id = b.patient_db_id
                JOIN public.users u
                    ON u.id = pr.created_by
                LEFT JOIN (
                    SELECT
                        payment_id,
                        SUM(refund_amount) AS total_refunded
                    FROM public.payment_refunds
                    GROUP BY payment_id
                ) refund_totals
                    ON refund_totals.payment_id = bp.id
                WHERE pr.id = %s
                """,
                (refund_id,),
            )

            refund = cursor.fetchone()

            if not refund:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="Refund not found.",
                )

        return serialize_row(refund)

    except HTTPException:
        raise
    except psycopg2.Error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Database error while retrieving refund details.",
        )
    finally:
        _close_connection(connection)
