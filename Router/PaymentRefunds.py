from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Optional

import psycopg2
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from pydantic import BaseModel, ConfigDict, Field
from psycopg2.extras import RealDictCursor

from Router.Login import get_current_user
from Utils.billing_helpers import get_db_connection, serialize_row

router = APIRouter(prefix="/api/payment-refunds", tags=["Payment Refunds"])


class CreatePaymentRefundRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bill_id: int = Field(gt=0)
    refund_amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    refund_method: str = Field(min_length=1, max_length=50)
    reason: str = Field(min_length=1, max_length=2000)
    notes: Optional[str] = Field(default=None, max_length=5000)


def _clean(value: str, name: str) -> str:
    value = value.strip()
    if not value:
        raise HTTPException(status_code=422, detail=f"{name} cannot be empty.")
    return value


def _validate_dates(date_from, date_to):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to.")


def _filters(search=None, bill_id=None, refund_method=None, date_from=None, date_to=None):
    clauses, params = ["1 = 1"], []
    if search and search.strip():
        term = f"%{search.strip()}%"
        clauses.append("""(
            pr.refund_number ILIKE %s OR b.bill_number ILIKE %s
            OR COALESCE(b.op_number, '') ILIKE %s OR COALESCE(b.ip_number, '') ILIKE %s
            OR p.name ILIKE %s OR COALESCE(p.patient_id, '') ILIKE %s
            OR pr.refund_method ILIKE %s OR pr.reason ILIKE %s
            OR COALESCE(pr.notes, '') ILIKE %s OR COALESCE(u.full_name, '') ILIKE %s
        )""")
        params.extend([term] * 10)
    if bill_id is not None:
        clauses.append("pr.bill_id = %s")
        params.append(bill_id)
    if refund_method:
        clauses.append("LOWER(pr.refund_method) = LOWER(%s)")
        params.append(refund_method.strip())
    if date_from:
        clauses.append("pr.created_at >= %s")
        params.append(datetime.combine(date_from, time.min))
    if date_to:
        clauses.append("pr.created_at < %s")
        params.append(datetime.combine(date_to + timedelta(days=1), time.min))
    return " AND ".join(clauses), params


def _bill_refund_balance(cursor, bill_id: int, lock=False):
    cursor.execute(
        f"SELECT id, bill_number, bill_type, total_amount FROM public.bills WHERE id = %s {'FOR UPDATE' if lock else ''}",
        (bill_id,),
    )
    bill = cursor.fetchone()
    if not bill:
        return None
    cursor.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total_paid
           FROM public.bill_payments WHERE bill_id = %s""",
        (bill_id,),
    )
    paid = Decimal(str(cursor.fetchone()["total_paid"] or 0))
    cursor.execute(
        "SELECT COALESCE(SUM(refund_amount), 0) AS total_refunded FROM public.payment_refunds WHERE bill_id = %s",
        (bill_id,),
    )
    refunded = Decimal(str(cursor.fetchone()["total_refunded"] or 0))
    total = Decimal(str(bill["total_amount"] or 0))
    outstanding = max(total - paid, Decimal("0.00"))
    refundable = max(paid - refunded, Decimal("0.00"))
    return {
        **dict(bill), "bill_total": total, "total_paid": paid,
        "total_refunded": refunded, "net_retained": max(paid - refunded, Decimal("0.00")),
        "outstanding_amount": outstanding, "refundable_balance": refundable,
        "payment_status": "Paid" if total <= 0 or paid >= total else ("Partially Paid" if paid > 0 else "Unpaid"),
        "refund_status": "Fully Refunded" if refunded > 0 and refunded >= paid else ("Partially Refunded" if refunded > 0 else "Not Refunded"),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
def create_payment_refund(payload: CreatePaymentRefundRequest, current_user: dict = Depends(get_current_user)):
    method = _clean(payload.refund_method, "refund_method")
    reason = _clean(payload.reason, "reason")
    notes = payload.notes.strip() if payload.notes and payload.notes.strip() else None
    connection = None
    try:
        connection = get_db_connection()
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                balance = _bill_refund_balance(cursor, payload.bill_id, lock=True)
                if not balance:
                    raise HTTPException(status_code=404, detail="Bill not found.")
                total = balance["bill_total"]
                paid = balance["total_paid"]
                refunded = balance["total_refunded"]
                refundable = balance["refundable_balance"]
                if refunded > 0:
                    raise HTTPException(
                        status_code=409,
                        detail="This bill has already been refunded. A bill can only be refunded once.",
                    )
                if total > 0 and paid < total:
                    raise HTTPException(status_code=400, detail="Refunds are allowed only after the bill has been paid in full.")
                if payload.refund_amount > refundable:
                    raise HTTPException(status_code=400, detail={
                        "message": "Refund amount exceeds the remaining refundable amount for this bill.",
                        "bill_total": str(total), "total_paid": str(paid),
                        "total_refunded": str(refunded), "refundable_balance": str(refundable),
                    })
                try:
                    created_by = int(current_user["user"]["id"])
                except (KeyError, TypeError, ValueError):
                    raise HTTPException(status_code=401, detail="Unable to identify the authenticated user.") from None
                cursor.execute("SELECT id FROM public.users WHERE id = %s", (created_by,))
                if not cursor.fetchone():
                    raise HTTPException(status_code=401, detail="Authenticated user was not found.")
                cursor.execute(
                    """INSERT INTO public.payment_refunds
                       (bill_id, refund_amount, refund_method, reason, notes, created_by)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       RETURNING id, refund_number, bill_id, refund_amount, refund_method,
                                 reason, notes, created_by, created_at""",
                    (payload.bill_id, payload.refund_amount, method, reason, notes, created_by),
                )
                refund = dict(cursor.fetchone())
                refreshed = _bill_refund_balance(cursor, payload.bill_id)
                return serialize_row({**refreshed, **refund, "bill_id": payload.bill_id,
                    "total_refunded_for_bill": refreshed["total_refunded"],
                    "net_retained": refreshed["net_retained"],
                })
    except HTTPException:
        raise
    except psycopg2.Error:
        raise HTTPException(status_code=500, detail="Database error while recording the refund.")
    finally:
        if connection is not None:
            connection.close()


@router.get("")
def list_payment_refunds(
    page: int = Query(default=1, ge=1), page_size: int = Query(default=10, ge=1, le=100),
    search: Optional[str] = Query(default=None, max_length=150),
    bill_id: Optional[int] = Query(default=None, gt=0),
    refund_method: Optional[str] = Query(default=None, max_length=50),
    date_from: Optional[date] = Query(default=None), date_to: Optional[date] = Query(default=None),
):
    _validate_dates(date_from, date_to)
    where, params = _filters(search, bill_id, refund_method, date_from, date_to)
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            base = """FROM public.payment_refunds pr JOIN public.bills b ON b.id = pr.bill_id
                       JOIN public.patients p ON p.id = b.patient_db_id
                       LEFT JOIN public.users u ON u.id = pr.created_by"""
            cursor.execute(f"SELECT COUNT(*) AS total_records {base} WHERE {where}", params)
            total_records = cursor.fetchone()["total_records"]
            cursor.execute(f"""SELECT pr.id, pr.refund_number, pr.bill_id, b.bill_number, b.bill_type,
                    b.total_amount AS bill_total, p.name AS patient_name, p.patient_id AS patient_uhid,
                    p.mobile AS patient_mobile, pr.refund_amount, pr.refund_method, pr.reason, pr.notes,
                    pr.created_by, u.full_name AS created_by_name, pr.created_at,
                    COALESCE(payments.total_paid, 0) AS total_paid,
                    COALESCE(refunds.total_refunded, 0) AS total_refunded,
                    GREATEST(b.total_amount - COALESCE(payments.total_paid, 0), 0) AS outstanding_amount,
                    GREATEST(COALESCE(payments.total_paid, 0) - COALESCE(refunds.total_refunded, 0), 0) AS net_retained
                {base}
                LEFT JOIN (SELECT bill_id, SUM(amount) AS total_paid FROM public.bill_payments GROUP BY bill_id) payments ON payments.bill_id = b.id
                LEFT JOIN (SELECT bill_id, SUM(refund_amount) AS total_refunded FROM public.payment_refunds GROUP BY bill_id) refunds ON refunds.bill_id = b.id
                WHERE {where} ORDER BY pr.created_at DESC, pr.id DESC LIMIT %s OFFSET %s""", params + [page_size, (page-1)*page_size])
            items = [serialize_row(dict(row)) for row in cursor.fetchall()]
        total_pages = (total_records + page_size - 1) // page_size if total_records else 0
        return {"items": items, "pagination": {"page": page, "page_size": page_size, "total_records": total_records, "total_pages": total_pages, "has_next": page < total_pages, "has_previous": page > 1 and total_pages > 0}}
    except psycopg2.Error:
        raise HTTPException(status_code=500, detail="Database error while retrieving refunds.")
    finally:
        if connection is not None: connection.close()


@router.get("/summary")
def get_payment_refund_summary(
    search: Optional[str] = Query(default=None, max_length=150), bill_id: Optional[int] = Query(default=None, gt=0),
    refund_method: Optional[str] = Query(default=None, max_length=50), date_from: Optional[date] = Query(default=None), date_to: Optional[date] = Query(default=None),
):
    _validate_dates(date_from, date_to)
    where, params = _filters(search, bill_id, refund_method, date_from, date_to)
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            base = """FROM public.payment_refunds pr JOIN public.bills b ON b.id = pr.bill_id
                       JOIN public.patients p ON p.id = b.patient_db_id
                       LEFT JOIN public.users u ON u.id = pr.created_by"""
            cursor.execute(f"""SELECT COUNT(*) AS refund_count, COALESCE(SUM(pr.refund_amount),0) AS total_refunded_amount,
                    COALESCE(AVG(pr.refund_amount),0) AS average_refund_amount,
                    COUNT(DISTINCT pr.bill_id) AS bills_with_refunds,
                    MIN(pr.created_at) AS first_refund_at, MAX(pr.created_at) AS latest_refund_at
                    {base} WHERE {where}""", params)
            summary = serialize_row(dict(cursor.fetchone()))
            cursor.execute(f"""SELECT pr.refund_method, COUNT(*) AS refund_count,
                    COALESCE(SUM(pr.refund_amount),0) AS total_refunded_amount
                    {base} WHERE {where} GROUP BY pr.refund_method ORDER BY total_refunded_amount DESC""", params)
            methods = [serialize_row(dict(row)) for row in cursor.fetchall()]
        return {**summary, "by_refund_method": methods}
    except psycopg2.Error:
        raise HTTPException(status_code=500, detail="Database error while retrieving refund summary.")
    finally:
        if connection is not None: connection.close()


@router.get("/bills/{bill_id}/balance")
def get_bill_refund_balance(bill_id: int = Path(gt=0)):
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            balance = _bill_refund_balance(cursor, bill_id)
            if not balance:
                raise HTTPException(status_code=404, detail="Bill not found.")
            return serialize_row(balance)
    except HTTPException:
        raise
    except psycopg2.Error:
        raise HTTPException(status_code=500, detail="Database error while retrieving bill refund balance.")
    finally:
        if connection is not None: connection.close()


@router.get("/{refund_id}")
def get_payment_refund_details(refund_id: int = Path(gt=0)):
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("""SELECT pr.id, pr.refund_number, pr.bill_id, b.bill_number, b.bill_type,
                    b.total_amount AS bill_total, p.name AS patient_name, p.patient_id AS patient_uhid,
                    p.mobile AS patient_mobile, pr.refund_amount, pr.refund_method, pr.reason, pr.notes,
                    pr.created_by, u.full_name AS created_by_name, pr.created_at,
                    COALESCE(payments.total_paid,0) AS total_paid, COALESCE(refunds.total_refunded,0) AS total_refunded,
                    GREATEST(b.total_amount - COALESCE(payments.total_paid,0),0) AS outstanding_amount,
                    GREATEST(COALESCE(payments.total_paid,0)-COALESCE(refunds.total_refunded,0),0) AS net_retained
                FROM public.payment_refunds pr JOIN public.bills b ON b.id=pr.bill_id
                JOIN public.patients p ON p.id=b.patient_db_id LEFT JOIN public.users u ON u.id=pr.created_by
                LEFT JOIN (SELECT bill_id,SUM(amount) AS total_paid FROM public.bill_payments GROUP BY bill_id) payments ON payments.bill_id=b.id
                LEFT JOIN (SELECT bill_id,SUM(refund_amount) AS total_refunded FROM public.payment_refunds GROUP BY bill_id) refunds ON refunds.bill_id=b.id
                WHERE pr.id=%s""", (refund_id,))
            row = cursor.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Refund not found.")
            return serialize_row(dict(row))
    except HTTPException:
        raise
    except psycopg2.Error:
        raise HTTPException(status_code=500, detail="Database error while retrieving refund details.")
    finally:
        if connection is not None: connection.close()
