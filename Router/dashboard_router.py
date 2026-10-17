
from datetime import date, datetime
from zoneinfo import ZoneInfo

import psycopg2
from fastapi import APIRouter, HTTPException, Request
from psycopg2.extras import RealDictCursor

from Core.logger import get_logger
from Utils.billing_helpers import get_db_connection

logger = get_logger(__name__)

router = APIRouter(
    prefix="/api/dashboard",
    tags=["Dashboard"],
)


@router.get("")
def get_dashboard(request: Request):
    """Return all dashboard data in one API response."""

    # Validate the existing authenticated session.
    from Router.Login import get_current_user

    auth_result = get_current_user(request)
    current_user = auth_result.get("user", {})
    email = current_user.get("email")

    if not email:
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
        )

    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            # Check the user's current dashboard permission.
            cursor.execute(
                """
                SELECT r.permissions
                FROM public.users u
                JOIN public.roles r ON r.id = u.role_id
                WHERE LOWER(u.email) = LOWER(%s)
                  AND u.is_active = TRUE
                """,
                (email,),
            )

            permission_row = cursor.fetchone()

            if permission_row is None:
                raise HTTPException(
                    status_code=403,
                    detail="User is inactive or not authorized.",
                )

            permissions = permission_row["permissions"] or {}

            if permissions.get("dashboard.view") is not True:
                raise HTTPException(
                    status_code=403,
                    detail="You do not have permission to view the dashboard.",
                )

            # 1. Today's OP and IP bill counts.
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE bill_type = 'OP'
                    ) AS op_bills_today,
                    COUNT(*) FILTER (
                        WHERE bill_type = 'IP'
                    ) AS ip_bills_today
                FROM public.bills
                WHERE bill_date >= CURRENT_DATE
                  AND bill_date < CURRENT_DATE + INTERVAL '1 day'
                """
            )

            bill_counts = cursor.fetchone()

            # 2. Patients registered today.
            # Correct column: registered_at, not created_at.
            cursor.execute(
                """
                SELECT COUNT(*) AS patients_registered_today
                FROM public.patients
                WHERE registered_at >= CURRENT_DATE
                  AND registered_at < CURRENT_DATE + INTERVAL '1 day'
                """
            )

            patient_counts = cursor.fetchone()

            # 3. Actual payments received and refunds issued today.
            cursor.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS total_payments_today
                FROM public.bill_payments
                WHERE payment_date >= CURRENT_DATE
                  AND payment_date < CURRENT_DATE + INTERVAL '1 day'
                """
            )
            payment_collection = cursor.fetchone()

            cursor.execute(
                """
                SELECT COALESCE(SUM(refund_amount), 0) AS refunds_today
                FROM public.payment_refunds
                WHERE created_at >= CURRENT_DATE
                  AND created_at < CURRENT_DATE + INTERVAL '1 day'
                """
            )
            refund_collection = cursor.fetchone()

            total_payments_today = float(
                payment_collection["total_payments_today"] or 0
            )
            refunds_today = float(refund_collection["refunds_today"] or 0)
            net_collection_today = total_payments_today - refunds_today

            # Shared payment and refund aggregations for bill balances.
            payment_totals_sql = """
                SELECT bill_id, SUM(amount) AS amount_paid
                FROM public.bill_payments
                GROUP BY bill_id
            """
            refund_totals_sql = """
                SELECT bill_id, SUM(refund_amount) AS total_refunds
                FROM public.payment_refunds
                GROUP BY bill_id
            """
            net_paid_sql = "COALESCE(pay.amount_paid, 0)"

            # 4. Outstanding balance across all bills.
            cursor.execute(
                f"""
                SELECT COALESCE(
                    SUM(
                        GREATEST(
                            b.total_amount - COALESCE(p.amount_paid, 0),
                            0
                        )
                    ),
                    0
                ) AS outstanding_amount
                FROM public.bills b
                LEFT JOIN ({payment_totals_sql}) p ON p.bill_id = b.id
                LEFT JOIN ({refund_totals_sql}) r ON r.bill_id = b.id
                """
            )

            outstanding = cursor.fetchone()

            # 5. Daily net collection trend for the last 7 days.
            cursor.execute(
                """
                WITH days AS (
                    SELECT generate_series(
                        CURRENT_DATE - INTERVAL '6 days',
                        CURRENT_DATE,
                        INTERVAL '1 day'
                    )::date AS day
                ), payments AS (
                    SELECT payment_date::date AS day, SUM(amount) AS amount
                    FROM public.bill_payments
                    WHERE payment_date >= CURRENT_DATE - INTERVAL '6 days'
                      AND payment_date < CURRENT_DATE + INTERVAL '1 day'
                    GROUP BY payment_date::date
                ), refunds AS (
                    SELECT created_at::date AS day, SUM(refund_amount) AS amount
                    FROM public.payment_refunds
                    WHERE created_at >= CURRENT_DATE - INTERVAL '6 days'
                      AND created_at < CURRENT_DATE + INTERVAL '1 day'
                    GROUP BY created_at::date
                )
                SELECT
                    d.day,
                    COALESCE(p.amount, 0) AS total_payments,
                    COALESCE(r.amount, 0) AS total_refunds,
                    COALESCE(p.amount, 0) - COALESCE(r.amount, 0) AS collection
                FROM days d
                LEFT JOIN payments p ON p.day = d.day
                LEFT JOIN refunds r ON r.day = d.day
                ORDER BY d.day
                """
            )

            collection_trend = [
                {
                    "date": row["day"].isoformat(),
                    "amount": float(row["collection"]),
                    "total_payments": float(row["total_payments"]),
                    "total_refunds": float(row["total_refunds"]),
                }
                for row in cursor.fetchall()
            ]

            # 6. OP vs IP bill counts and amounts for the last 7 days.
            cursor.execute(
                """
                WITH days AS (
                    SELECT generate_series(
                        CURRENT_DATE - INTERVAL '6 days',
                        CURRENT_DATE,
                        INTERVAL '1 day'
                    )::date AS day
                )
                SELECT
                    d.day,
                    COUNT(b.id) FILTER (
                        WHERE b.bill_type = 'OP'
                    ) AS op_bill_count,
                    COUNT(b.id) FILTER (
                        WHERE b.bill_type = 'IP'
                    ) AS ip_bill_count,
                    COALESCE(
                        SUM(b.total_amount) FILTER (
                            WHERE b.bill_type = 'OP'
                        ), 0
                    ) AS op_bill_amount,
                    COALESCE(
                        SUM(b.total_amount) FILTER (
                            WHERE b.bill_type = 'IP'
                        ), 0
                    ) AS ip_bill_amount
                FROM days d
                LEFT JOIN public.bills b
                    ON b.bill_date >= d.day
                   AND b.bill_date < d.day + INTERVAL '1 day'
                GROUP BY d.day
                ORDER BY d.day
                """
            )

            billing_trend = [
                {
                    "date": row["day"].isoformat(),
                    "op_bill_count": row["op_bill_count"],
                    "ip_bill_count": row["ip_bill_count"],
                    "op_bill_amount": float(row["op_bill_amount"]),
                    "ip_bill_amount": float(row["ip_bill_amount"]),
                }
                for row in cursor.fetchall()
            ]

            # 7. Current bill payment-status breakdown.
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) FILTER (
                        WHERE b.total_amount <= 0
                           OR {net_paid_sql} >= b.total_amount
                    ) AS paid,
                    COUNT(*) FILTER (
                        WHERE {net_paid_sql} > 0
                          AND {net_paid_sql} < b.total_amount
                    ) AS partially_paid,
                    COUNT(*) FILTER (
                        WHERE {net_paid_sql} <= 0
                          AND b.total_amount > 0
                    ) AS unpaid
                FROM public.bills b
                LEFT JOIN ({payment_totals_sql}) pay ON pay.bill_id = b.id
                LEFT JOIN ({refund_totals_sql}) ref ON ref.bill_id = b.id
                """
            )

            payment_status = cursor.fetchone()

            # 8. Ten most recent bills.
            cursor.execute(
                f"""
                SELECT
                    b.id,
                    b.bill_number,
                    b.bill_type,
                    b.bill_date,
                    b.total_amount,
                    p.patient_id,
                    p.name AS patient_name,
                    COALESCE(pay.amount_paid, 0) AS total_payments,
                    COALESCE(ref.total_refunds, 0) AS total_refunds,
                    {net_paid_sql} AS amount_paid,
                    GREATEST(
                        b.total_amount - ({net_paid_sql}),
                        0
                    ) AS outstanding_amount,
                    CASE
                        WHEN b.total_amount <= 0
                          OR ({net_paid_sql}) >= b.total_amount
                            THEN 'Paid'
                        WHEN ({net_paid_sql}) > 0
                            THEN 'Partially Paid'
                        ELSE 'Unpaid'
                    END AS payment_status
                FROM public.bills b
                JOIN public.patients p ON p.id = b.patient_db_id
                LEFT JOIN ({payment_totals_sql}) pay ON pay.bill_id = b.id
                LEFT JOIN ({refund_totals_sql}) ref ON ref.bill_id = b.id
                ORDER BY b.bill_date DESC, b.id DESC
                LIMIT 10
                """
            )

            recent_bills = []

            for row in cursor.fetchall():
                recent_bills.append(
                    {
                        "id": row["id"],
                        "bill_number": row["bill_number"],
                        "bill_type": row["bill_type"],
                        "bill_date": (
                            row["bill_date"].isoformat()
                            if row["bill_date"]
                            else None
                        ),
                        "patient_id": row["patient_id"],
                        "patient_name": row["patient_name"],
                        "total_amount": float(row["total_amount"]),
                        "total_payments": float(row["total_payments"]),
                        "total_refunds": float(row["total_refunds"]),
                        "amount_paid": float(row["amount_paid"]),
                        "outstanding_amount": float(
                            row["outstanding_amount"]
                        ),
                        "payment_status": row["payment_status"],
                    }
                )

            return {
                "summary": {
                    "patients_registered_today": patient_counts[
                        "patients_registered_today"
                    ],
                    "op_bills_today": bill_counts["op_bills_today"],
                    "ip_bills_today": bill_counts["ip_bills_today"],
                    "total_payments_today": total_payments_today,
                    "refunds_today": refunds_today,
                    "net_collection_today": net_collection_today,
                    # Backward-compatible field now reflects net collection.
                    "collection_today": net_collection_today,
                    "outstanding_amount": float(
                        outstanding["outstanding_amount"]
                    ),
                },
                "charts": {
                    "collection_trend": collection_trend,
                    "billing_trend": billing_trend,
                    "payment_status": {
                        "paid": payment_status["paid"],
                        "partially_paid": payment_status["partially_paid"],
                        "unpaid": payment_status["unpaid"],
                    },
                },
                "recent_bills": recent_bills,
                "generated_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(),
            }

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Database error loading dashboard.")
        raise HTTPException(
            status_code=500,
            detail="Unable to load dashboard data.",
        ) from None

    except Exception:
        logger.exception("Unexpected error loading dashboard.")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while loading the dashboard.",
        ) from None

    finally:
        if connection is not None:
            connection.close()
