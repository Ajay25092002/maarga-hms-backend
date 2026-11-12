
"""Read-only reporting queries for OP/IP Billing."""

from typing import Any
from zoneinfo import ZoneInfo

from psycopg2.extras import RealDictCursor

from Utils.billing_helpers import get_db_connection, serialize_row


REPORTS = {
    "patient-registration": "Patient Registration",
    "op-billing": "OP Billing",
    "ip-billing": "IP Billing",
    "cash-collection": "Cash Collection",
    "refunds": "Refunds",
    "outstanding": "Partially Paid / Outstanding Bills",
    "service-wise": "Service-wise Report",
    "service-group-wise": "Service Group-wise Report",
    "admission-discharge": "Admission & Discharge",
    "source-of-lead": "Source of Lead",
    "accrued-revenue": "Accrued Revenue",
}


# Shared financial formula for OP and IP billing:
#
# gross_paid       = original payments received
# total_refunded   = refunds recorded against the bill
# net_paid         = gross_paid - total_refunded
# outstanding      = total_amount - gross_paid
# deposit_balance  = net_paid - total_amount
#
# Refunds affect net_paid, but do not reopen a fully paid bill.

BASE_SELECTS = {
    "patient-registration": """
        SELECT
            p.registered_at AS report_date,
            p.patient_id AS "Patient Number",
            p.name AS "Patient Name",
            p.mobile AS "Mobile",
            p.gender AS "Gender",
            p.date_of_birth AS "Date of Birth",
            COALESCE(
                NULLIF(p.source_of_lead, ''),
                'Not Specified'
            ) AS "Source of Lead",
            p.registered_at AS "Registration Date"
        FROM public.patients p
    """,

    "op-billing": """
        WITH bill_financials AS (
            SELECT
                b.id AS bill_id,
                b.bill_number,
                b.op_number,
                b.patient_db_id,
                b.bill_date,
                b.created_at,
                b.total_amount,
                b.created_by,

                COALESCE(pay.gross_paid, 0) AS gross_paid,
                COALESCE(ref.total_refunded, 0) AS total_refunded,

                GREATEST(
                    COALESCE(pay.gross_paid, 0)
                    - COALESCE(ref.total_refunded, 0),
                    0
                ) AS net_paid,

                GREATEST(
                    COALESCE(b.total_amount, 0)
                    - COALESCE(pay.gross_paid, 0),
                    0
                ) AS outstanding_amount,

                GREATEST(
                    COALESCE(pay.gross_paid, 0)
                    - COALESCE(ref.total_refunded, 0)
                    - COALESCE(b.total_amount, 0),
                    0
                ) AS deposit_balance

            FROM public.bills b

            LEFT JOIN (
                SELECT
                    bill_id,
                    SUM(amount) AS gross_paid
                FROM public.bill_payments
                GROUP BY bill_id
            ) pay ON pay.bill_id = b.id

            LEFT JOIN (
                SELECT
                    bill_id,
                    SUM(refund_amount) AS total_refunded
                FROM public.payment_refunds
                GROUP BY bill_id
            ) ref ON ref.bill_id = b.id

            WHERE b.bill_type = 'OP'
        ),

        transactions AS (
            SELECT
                bp.bill_id,
                bp.payment_date AS transaction_date,
                'Payment'::text AS transaction_type,
                bp.receipt_number AS transaction_reference,
                bp.payment_method AS payment_mode,
                bp.amount AS payment_amount,
                NULL::numeric AS refund_amount,
                bp.created_at AS recorded_at
            FROM public.bill_payments bp
            JOIN bill_financials bf
                ON bf.bill_id = bp.bill_id

            UNION ALL

            SELECT
                pr.bill_id,
                pr.created_at AS transaction_date,
                'Refund'::text AS transaction_type,
                pr.refund_number AS transaction_reference,
                NULL::varchar AS payment_mode,
                NULL::numeric AS payment_amount,
                pr.refund_amount AS refund_amount,
                pr.created_at AS recorded_at
            FROM public.payment_refunds pr
            JOIN bill_financials bf
                ON bf.bill_id = pr.bill_id

            UNION ALL

            SELECT
                bf.bill_id,
                bf.bill_date AS transaction_date,
                'Payment'::text AS transaction_type,
                NULL::varchar AS transaction_reference,
                NULL::varchar AS payment_mode,
                NULL::numeric AS payment_amount,
                NULL::numeric AS refund_amount,
                bf.bill_date AS recorded_at
            FROM bill_financials bf
            WHERE NOT EXISTS (
                SELECT 1
                FROM public.bill_payments bp
                WHERE bp.bill_id = bf.bill_id
            )
            AND NOT EXISTS (
                SELECT 1
                FROM public.payment_refunds pr
                WHERE pr.bill_id = bf.bill_id
            )
        ),

        ordered_transactions AS (
            SELECT
                t.*,
                ROW_NUMBER() OVER (
                    PARTITION BY t.bill_id
                    ORDER BY
                        t.transaction_date,
                        t.transaction_type,
                        t.transaction_reference NULLS FIRST
                ) AS transaction_row
            FROM transactions t
        )

        SELECT
            bf.created_at AS report_date,
            bf.bill_number AS "Bill Number",
            bf.op_number AS "OP Number",
            p.patient_id AS "Patient Number",
            p.name AS "Patient Name",
            bf.bill_date AS "Bill Date",

            ot.transaction_date AS "Transaction Date/Time",
            ot.transaction_type AS "Transaction Type",
            ot.transaction_reference AS "Receipt / Refund ID",
            ot.payment_mode AS "Payment Mode",
            ot.payment_amount AS "Payment Amount",
            ot.refund_amount AS "Refund Amount",

            CASE
                WHEN ot.transaction_row = 1
                THEN bf.total_amount
            END AS "Total Bill Amount",

            CASE
                WHEN ot.transaction_row = 1
                THEN bf.gross_paid
            END AS "Amount Paid",

            CASE
                WHEN ot.transaction_row = 1
                THEN bf.total_refunded
            END AS "Total Refunded",

            CASE
                WHEN ot.transaction_row = 1
                THEN bf.net_paid
            END AS "Net Paid",

            CASE
                WHEN ot.transaction_row = 1
                THEN bf.outstanding_amount
            END AS "Outstanding Amount",

            CASE
                WHEN COALESCE(bf.total_amount, 0) <= 0
                    THEN 'Paid'
                WHEN bf.gross_paid <= 0
                    THEN 'Unpaid'
                WHEN bf.gross_paid < bf.total_amount
                    THEN 'Partially Paid'
                ELSE 'Paid'
            END AS "Payment Status",

            COALESCE(u.full_name, '—') AS "Billed By",
            ot.recorded_at AS "Transaction Recorded At"

        FROM ordered_transactions ot
        JOIN bill_financials bf
            ON bf.bill_id = ot.bill_id
        JOIN public.patients p
            ON p.id = bf.patient_db_id
        LEFT JOIN public.users u
            ON u.id = bf.created_by
    """,

    "cash-collection": """
        SELECT
            bp.payment_date AS report_date,
            bp.receipt_number AS "Receipt Number",
            b.bill_number AS "Bill Number",
            CASE
                WHEN b.bill_type = 'OP' THEN b.op_number
                ELSE b.ip_number
            END AS "OP/IP Number",
            b.bill_type AS "Bill Type",
            p.name AS "Patient Name",
            bp.payment_date AS "Payment Date",
            bp.payment_method AS "Payment Method",
            bp.amount AS "Payment Amount",
            COALESCE(u.full_name, '—') AS "Collected By"
        FROM public.bill_payments bp
        JOIN public.bills b
            ON b.id = bp.bill_id
        JOIN public.patients p
            ON p.id = b.patient_db_id
        LEFT JOIN public.users u
            ON u.id = bp.created_by
    """,

    "refunds": """
        SELECT
            pr.created_at AS report_date,
            pr.refund_number AS "Refund Number",
            b.bill_number AS "Bill Number",
            CASE
                WHEN b.bill_type = 'OP' THEN b.op_number
                ELSE b.ip_number
            END AS "OP/IP Number",
            b.bill_type AS "Bill Type",
            p.name AS "Patient Name",
            pr.created_at AS "Refund Date",
            pr.refund_amount AS "Refund Amount",
            pr.refund_method AS "Refund Method",
            pr.reason AS "Reason",
            COALESCE(u.full_name, '—') AS "Refunded By"
        FROM public.payment_refunds pr
        JOIN public.bills b
            ON b.id = pr.bill_id
        JOIN public.patients p
            ON p.id = b.patient_db_id
        LEFT JOIN public.users u
            ON u.id = pr.created_by
    """,

    "outstanding": """
        WITH bill_financials AS (
            SELECT
                b.id AS bill_id,
                b.bill_date,
                b.created_at,
                b.bill_number,
                b.bill_type,
                b.op_number,
                b.ip_number,
                b.patient_db_id,
                b.total_amount,

                COALESCE(pay.gross_paid, 0) AS gross_paid,

                GREATEST(
                    COALESCE(pay.gross_paid, 0)
                    - COALESCE(ref.total_refunded, 0),
                    0
                ) AS net_paid,

                GREATEST(
                    COALESCE(b.total_amount, 0)
                    - COALESCE(pay.gross_paid, 0),
                    0
                ) AS outstanding_amount,

                pay.last_payment

            FROM public.bills b

            LEFT JOIN (
                SELECT
                    bill_id,
                    SUM(amount) AS gross_paid,
                    MAX(payment_date) AS last_payment
                FROM public.bill_payments
                GROUP BY bill_id
            ) pay ON pay.bill_id = b.id

            LEFT JOIN (
                SELECT
                    bill_id,
                    SUM(refund_amount) AS total_refunded
                FROM public.payment_refunds
                GROUP BY bill_id
            ) ref ON ref.bill_id = b.id
        )

        SELECT
            bf.created_at AS report_date,
            bf.bill_number AS "Bill Number",
            bf.bill_type AS "Bill Type",

            CASE
                WHEN bf.bill_type = 'OP' THEN bf.op_number
                ELSE bf.ip_number
            END AS "OP/IP Number",

            p.patient_id AS "Patient Number",
            p.name AS "Patient Name",
            bf.bill_date AS "Bill Date",
            bf.total_amount AS "Bill Amount",
            bf.gross_paid AS "Amount Paid",
            bf.outstanding_amount AS "Outstanding Amount",

            CASE
                WHEN COALESCE(bf.total_amount, 0) <= 0
                    THEN 'Paid'
                WHEN bf.gross_paid <= 0
                    THEN 'Unpaid'
                WHEN bf.gross_paid < bf.total_amount
                    THEN 'Partially Paid'
                ELSE 'Paid'
            END AS "Payment Status",

            bf.last_payment AS "Last Payment Date"

        FROM bill_financials bf
        JOIN public.patients p
            ON p.id = bf.patient_db_id

        WHERE bf.outstanding_amount > 0
    """,

    "service-wise": """
        SELECT
            b.created_at::date AS report_date,
            b.bill_type AS "Bill Type",
            bi.service_name AS "Service Name",
            bi.service_group_name AS "Service Group",
            SUM(bi.quantity) AS "Quantity",
            SUM(bi.net_amount) AS "Total Billed Amount",
            COUNT(DISTINCT b.id) AS "Number of Bills"
        FROM public.bill_items bi
        JOIN public.bills b
            ON b.id = bi.bill_id
        GROUP BY
            b.created_at::date,
            b.bill_type,
            bi.service_name,
            bi.service_group_name
    """,

    "service-group-wise": """
        SELECT
            b.created_at::date AS report_date,
            b.bill_type AS "Bill Type",
            bi.service_group_name AS "Service Group",
            COUNT(DISTINCT bi.service_name) AS "Number of Services",
            SUM(bi.quantity) AS "Total Quantity",
            COUNT(DISTINCT b.id) AS "Number of Bills",
            SUM(bi.net_amount) AS "Total Billed Amount"
        FROM public.bill_items bi
        JOIN public.bills b
            ON b.id = bi.bill_id
        GROUP BY
            b.created_at::date,
            b.bill_type,
            bi.service_group_name
    """,

    "admission-discharge": """
        SELECT
            b.admission_date AS report_date,
            'Admission'::text AS "Event Type",
            b.admission_date::date AS "Event Date",
            b.bill_number AS "Bill Number",
            b.ip_number AS "IP Number",
            p.patient_id AS "Patient Number",
            p.name AS "Patient Name",
            b.admission_date::date AS "Admission Date",
            b.discharge_date::date AS "Discharge Date",
            CASE
                WHEN b.discharge_date IS NULL
                    THEN 'Currently Admitted'
                ELSE 'Discharged'
            END AS "Patient Status",
            CASE
                WHEN b.discharge_date IS NOT NULL
                    THEN GREATEST(
                        0,
                        b.discharge_date::date - b.admission_date::date
                    )
                ELSE NULL
            END AS "Length of Stay (Days)"
        FROM public.bills b
        JOIN public.patients p
            ON p.id = b.patient_db_id
        WHERE b.bill_type = 'IP'
          AND b.admission_date IS NOT NULL

        UNION ALL

        SELECT
            b.discharge_date AS report_date,
            'Discharge'::text AS "Event Type",
            b.discharge_date::date AS "Event Date",
            b.bill_number AS "Bill Number",
            b.ip_number AS "IP Number",
            p.patient_id AS "Patient Number",
            p.name AS "Patient Name",
            b.admission_date::date AS "Admission Date",
            b.discharge_date::date AS "Discharge Date",
            'Discharged'::text AS "Patient Status",
            CASE
                WHEN b.admission_date IS NOT NULL
                    THEN GREATEST(
                        0,
                        b.discharge_date::date - b.admission_date::date
                    )
                ELSE NULL
            END AS "Length of Stay (Days)"
        FROM public.bills b
        JOIN public.patients p
            ON p.id = b.patient_db_id
        WHERE b.bill_type = 'IP'
          AND b.discharge_date IS NOT NULL
    """,

    "source-of-lead": """
        SELECT
            p.registered_at AS report_date,

            COALESCE(
                NULLIF(p.source_of_lead, ''),
                'Not Specified'
            ) AS "Source of Lead",

            COUNT(*) OVER (
                PARTITION BY COALESCE(
                    NULLIF(p.source_of_lead, ''),
                    'Not Specified'
                )
            ) AS "Patient Count",

            ROUND(
                COUNT(*) OVER (
                    PARTITION BY COALESCE(
                        NULLIF(p.source_of_lead, ''),
                        'Not Specified'
                    )
                ) * 100.0
                / NULLIF(COUNT(*) OVER (), 0),
                2
            ) AS "Percentage",

            p.registered_at AS "Registration Date",
            p.patient_id AS "Patient Number",
            p.name AS "Patient Name",
            p.mobile AS "Mobile"

        FROM public.patients p
    """,
}



def _fetch_accrued_revenue_report(
    filters: dict[str, Any],
    page: int,
    page_size: int,
    export_all: bool = False,
):
    """Fetch accrued revenue using bill-item service dates, never bill dates."""
    year = int(filters["year"])
    month = int(filters["month"])
    start_date = f"{year:04d}-{month:02d}-01"
    end_date = f"{year + 1:04d}-01-01" if month == 12 else f"{year:04d}-{month + 1:02d}-01"

    based_on = filters.get("based_on") or "patient-wise"
    if based_on not in {"patient-wise", "bill-wise"}:
        raise ValueError("Based on must be patient-wise or bill-wise.")

    item_filters = [
        "bi.service_date >= %s::date",
        "bi.service_date < %s::date",
    ]
    params: list[Any] = [start_date, end_date]

    bill_type = filters.get("bill_type")
    if based_on == "bill-wise" and bill_type:
        item_filters.append("b.bill_type = %s")
        params.append(str(bill_type).upper())

    item_where = " AND ".join(item_filters)

    if based_on == "bill-wise":
        base = f"""
            WITH service_items AS (
                SELECT
                    bi.id AS bill_item_id,
                    bi.bill_id,
                    bi.service_date,
                    bi.service_name,
                    bi.amount,
                    bi.discount_amount,
                    bi.net_amount,
                    b.bill_number,
                    b.bill_type,
                    b.op_number,
                    b.ip_number,
                    b.bill_date,
                    b.admission_date,
                    b.discharge_date,
                    p.patient_id,
                    p.name AS patient_name,
                    p.mobile,
                    p.gender
                FROM public.bill_items bi
                JOIN public.bills b ON b.id = bi.bill_id
                JOIN public.patients p ON p.id = b.patient_db_id
                WHERE {item_where}
            )
            SELECT
                MAX(service_date) AS report_date,
                bill_number AS "Bill Number",
                bill_type AS "Bill Type",
                CASE WHEN bill_type = 'OP' THEN op_number ELSE ip_number END AS "OP/IP Number",
                patient_id AS "Patient Number",
                patient_name AS "Patient Name",
                mobile AS "Mobile",
                gender AS "Gender",
                bill_date AS "Bill Date",
                admission_date::date AS "Admission Date",
                discharge_date::date AS "Discharge Date",
                STRING_AGG(
                    DISTINCT service_name || ' (' || TO_CHAR(service_date, 'DD Mon YYYY') || ')',
                    ', ' ORDER BY service_name || ' (' || TO_CHAR(service_date, 'DD Mon YYYY') || ')'
                ) AS "Services",
                COUNT(*) AS "Service Item Count",
                COALESCE(SUM(amount), 0) AS "Amount",
                COALESCE(SUM(discount_amount), 0) AS "Discount",
                COALESCE(SUM(net_amount), 0) AS "Net Amount"
            FROM service_items
            GROUP BY
                bill_id, bill_number, bill_type, op_number, ip_number,
                patient_id, patient_name, mobile, gender, bill_date,
                admission_date, discharge_date
        """
    else:
        base = f"""
            WITH service_items AS (
                SELECT
                    bi.bill_id,
                    bi.service_date,
                    bi.amount,
                    bi.discount_amount,
                    bi.net_amount,
                    b.bill_number,
                    p.id AS patient_db_id,
                    p.patient_id,
                    p.name AS patient_name,
                    p.mobile,
                    p.gender
                FROM public.bill_items bi
                JOIN public.bills b ON b.id = bi.bill_id
                JOIN public.patients p ON p.id = b.patient_db_id
                WHERE {item_where}
            )
            SELECT
                MAX(service_date) AS report_date,
                patient_id AS "Patient Number",
                patient_name AS "Patient Name",
                mobile AS "Mobile",
                gender AS "Gender",
                COUNT(DISTINCT bill_id) AS "Number of Bills",
                STRING_AGG(DISTINCT bill_number, ', ' ORDER BY bill_number) AS "Bill Numbers",
                COALESCE(SUM(amount), 0) AS "Amount",
                COALESCE(SUM(discount_amount), 0) AS "Discount",
                COALESCE(SUM(net_amount), 0) AS "Net Amount"
            FROM service_items
            GROUP BY patient_db_id, patient_id, patient_name, mobile, gender
        """

    where = []
    query_params = list(params)
    if filters.get("search"):
        where.append("CAST(row_to_json(report_rows) AS TEXT) ILIKE %s")
        query_params.append(f"%{filters['search']}%")

    filtered_query = f"SELECT * FROM ({base}) report_rows"
    if where:
        filtered_query += " WHERE " + " AND ".join(where)

    connection = get_db_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            summary_query = f"""
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM("Amount"), 0) AS amount,
                    COALESCE(SUM("Discount"), 0) AS discount,
                    COALESCE(SUM("Net Amount"), 0) AS net_amount
                FROM ({filtered_query}) accrued_rows
            """
            cursor.execute(summary_query, query_params)
            summary = cursor.fetchone()
            total = int(summary["total"] or 0)
            totals = {
                "Amount": round(float(summary["amount"] or 0), 2),
                "Discount": round(float(summary["discount"] or 0), 2),
                "Net Amount": round(float(summary["net_amount"] or 0), 2),
            }

            data_query = filtered_query + " ORDER BY report_date DESC NULLS LAST"
            data_params = list(query_params)
            if not export_all:
                data_query += " LIMIT %s OFFSET %s"
                data_params.extend([page_size, (page - 1) * page_size])

            cursor.execute(data_query, data_params)
            items = [serialize_row(row) for row in cursor.fetchall()]

            return {
                "report_type": "accrued-revenue",
                "report_name": REPORTS["accrued-revenue"],
                "items": items,
                "total": total,
                "page": page,
                "page_size": total if export_all else page_size,
                "totals": totals,
                "based_on": based_on,
                "year": year,
                "month": month,
            }
    finally:
        connection.close()



def _fetch_ip_billing_report(
    filters: dict[str, Any],
    page: int,
    page_size: int,
    export_all: bool = False,
):
    """Return one row per IP bill service item, filtered only by service_date."""
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    if not date_from or not date_to:
        raise ValueError("Both start date and end date are required for the IP billing report.")
    if date_from > date_to:
        raise ValueError("Start date must be on or before end date.")

    base = """
        SELECT
            bi.service_date AS report_date,
            'IP'::varchar AS "Bill Type",
            b.bill_number AS "Bill Number",
            b.ip_number AS "IP Number",
            b.bill_date AS "Bill Date",
            COALESCE(NULLIF(BTRIM(b.sponsor_name_snapshot), ''), '—') AS "Sponsor Name",

            p.id AS "Patient ID",
            p.patient_id AS "Patient Unique Number",
            p.name AS "Patient Name",
            p.gender AS "Gender",
            CASE
                WHEN p.date_of_birth IS NOT NULL
                THEN EXTRACT(YEAR FROM AGE(bi.service_date, p.date_of_birth))::integer
                ELSE NULL
            END AS "Age",
            p.mobile AS "Mobile Number",

            bi.service_date AS "Service Date",
            bi.service_group_name AS "Service Group",
            bi.service_name AS "Service Name",
            bi.quantity AS "Quantity",
            bi.rate AS "Rate",
            bi.amount AS "Gross Amount",
            bi.discount_amount AS "Discount Amount",
            bi.gst_amount AS "GST Amount",
            bi.net_amount AS "Net Amount",

            b.department AS "Department",
            COALESCE(NULLIF(BTRIM(b.doctor_name), ''), '—') AS "Doctor Name",
            b.admission_date::date AS "Admission Date",
            b.discharge_date::date AS "Discharge Date",
            COALESCE(NULLIF(BTRIM(bi.room_type_name), ''), NULLIF(BTRIM(b.room_type_name), ''), rt.name, '—') AS "Room Type",
            COALESCE(NULLIF(BTRIM(bi.bed_number), ''), NULLIF(BTRIM(b.bed_number), ''), '—') AS "Bed Number",
            b.notes AS "Bill Notes"

        FROM public.bill_items bi
        JOIN public.bills b
            ON b.id = bi.bill_id
        JOIN public.patients p
            ON p.id = b.patient_db_id
        LEFT JOIN public.room_types rt
            ON rt.id = b.room_type_id
        WHERE
            b.bill_type = 'IP'
            AND bi.service_date >= %s::date
            AND bi.service_date <= %s::date
    """

    params: list[Any] = [date_from, date_to]
    filtered_query = f"SELECT * FROM ({base}) report_rows"
    if filters.get("search"):
        filtered_query += " WHERE CAST(row_to_json(report_rows) AS TEXT) ILIKE %s"
        params.append(f"%{filters['search']}%")

    connection = get_db_connection()
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            summary_query = f"""
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM("Gross Amount"), 0) AS gross_amount,
                    COALESCE(SUM("Discount Amount"), 0) AS discount_amount,
                    COALESCE(SUM("GST Amount"), 0) AS gst_amount,
                    COALESCE(SUM("Net Amount"), 0) AS net_amount
                FROM ({filtered_query}) ip_rows
            """
            cursor.execute(summary_query, params)
            summary = cursor.fetchone()
            total = int(summary["total"] or 0)
            totals = {
                "Gross Amount": round(float(summary["gross_amount"] or 0), 2),
                "Discount Amount": round(float(summary["discount_amount"] or 0), 2),
                "GST Amount": round(float(summary["gst_amount"] or 0), 2),
                "Net Amount": round(float(summary["net_amount"] or 0), 2),
            }

            data_query = (
                filtered_query
                + ' ORDER BY "Service Date" ASC, "Bill Number" ASC, "Service Name" ASC'
            )
            data_params = list(params)
            if not export_all:
                data_query += " LIMIT %s OFFSET %s"
                data_params.extend([page_size, (page - 1) * page_size])

            cursor.execute(data_query, data_params)
            items = [serialize_row(row) for row in cursor.fetchall()]

            return {
                "report_type": "ip-billing",
                "report_name": REPORTS["ip-billing"],
                "items": items,
                "total": total,
                "page": page,
                "page_size": total if export_all else page_size,
                "totals": totals,
                "date_from": date_from,
                "date_to": date_to,
            }
    finally:
        connection.close()


def fetch_report(
    report_type: str,
    filters: dict[str, Any],
    page: int,
    page_size: int,
    export_all: bool = False,
):
    if report_type not in REPORTS:
        raise ValueError("Unknown report type.")

    if report_type == "accrued-revenue":
        if not filters.get("year") or not filters.get("month"):
            raise ValueError("Year and month are required for the accrued revenue report.")
        return _fetch_accrued_revenue_report(filters, page, page_size, export_all)

    if report_type == "ip-billing":
        return _fetch_ip_billing_report(filters, page, page_size, export_all)

    base = BASE_SELECTS[report_type]
    where = []
    params: list[Any] = []

    # Admission & Discharge is event-based: each row represents exactly one
    # admission or discharge event, and report_date is that event's own date.
    if filters.get("date_from"):
        where.append("report_date::date >= %s::date")
        params.append(filters["date_from"])

    if filters.get("date_to"):
        where.append("report_date::date <= %s::date")
        params.append(filters["date_to"])

    if (
        filters.get("bill_type")
        and report_type in {
            "cash-collection",
            "refunds",
            "outstanding",
            "service-wise",
            "service-group-wise",
        }
    ):
        where.append('"Bill Type" = %s')
        params.append(filters["bill_type"].upper())

    if filters.get("payment_method") and report_type == "cash-collection":
        where.append('LOWER("Payment Method") = LOWER(%s)')
        params.append(filters["payment_method"])

    if filters.get("staff") and report_type == "cash-collection":
        where.append('"Collected By" ILIKE %s')
        params.append(f"%{filters['staff']}%")

    if (
        filters.get("source")
        and report_type in {"patient-registration", "source-of-lead"}
    ):
        where.append('"Source of Lead" = %s')
        params.append(filters["source"])

    if filters.get("service") and report_type == "service-wise":
        where.append('"Service Name" ILIKE %s')
        params.append(f"%{filters['service']}%")

    if (
        filters.get("service_group")
        and report_type in {"service-wise", "service-group-wise"}
    ):
        where.append('"Service Group" ILIKE %s')
        params.append(f"%{filters['service_group']}%")

    if filters.get("status") and report_type == "admission-discharge":
        if filters["status"] == "admission":
            where.append('"Event Type" = %s')
            params.append("Admission")
        elif filters["status"] == "discharge":
            where.append('"Event Type" = %s')
            params.append("Discharge")

    if filters.get("search"):
        where.append("CAST(row_to_json(report_rows) AS TEXT) ILIKE %s")
        params.append(f"%{filters['search']}%")

    query = f"SELECT * FROM ({base}) report_rows"

    if where:
        query += " WHERE " + " AND ".join(where)

    query += " ORDER BY report_date DESC NULLS LAST"

    connection = get_db_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            count_query = f"SELECT COUNT(*) AS total FROM ({query}) count_rows"
            cursor.execute(count_query, params)
            total = cursor.fetchone()["total"]

            numeric_keys = [
                "Bill Amount",
                "Total Bill Amount",
                "Amount Paid",
                "Total Refunded",
                "Net Paid",
                "Deposit Balance",
                "Outstanding Amount",
                "Payment Amount",
                "Refund Amount",
                "Total Billed Amount",
                "Debit Balance",
            ]
            cursor.execute(f"SELECT * FROM ({query}) report_rows LIMIT 0", params)
            available_columns = {description[0] for description in cursor.description}
            total_columns = [key for key in numeric_keys if key in available_columns]
            totals = {}
            if total_columns:
                aggregate_select = ", ".join(
                    f'SUM(COALESCE("{key}", 0)) AS "{key}"'
                    for key in total_columns
                )
                totals_query = f"SELECT {aggregate_select} FROM ({query}) report_rows"
                cursor.execute(totals_query, params)
                totals_row = cursor.fetchone()
                totals = {
                    key: round(float(totals_row[key] or 0), 2)
                    for key in total_columns
                }

            query_params = list(params)

            if not export_all:
                query += " LIMIT %s OFFSET %s"
                query_params.extend([
                    page_size,
                    (page - 1) * page_size,
                ])

            cursor.execute(query, query_params)
            items = [
                serialize_row(row)
                for row in cursor.fetchall()
            ]

            return {
                "report_type": report_type,
                "report_name": REPORTS[report_type],
                "items": items,
                "total": total,
                "page": page,
                "page_size": total if export_all else page_size,
                "totals": totals,
            }

    finally:
        connection.close()


def _build_streaming_report_query(report_type: str, filters: dict[str, Any]):
    """Build the same filtered, ordered dataset used by fetch_report, without pagination."""
    if report_type == "ip-billing":
        date_from = filters.get("date_from")
        date_to = filters.get("date_to")
        if not date_from or not date_to:
            raise ValueError("Both start date and end date are required for the IP billing report.")
        if date_from > date_to:
            raise ValueError("Start date must be on or before end date.")
        base = """
            SELECT
                bi.service_date AS report_date,
                'IP'::varchar AS "Bill Type",
                b.bill_number AS "Bill Number",
                b.ip_number AS "IP Number",
                b.bill_date AS "Bill Date",
                COALESCE(NULLIF(BTRIM(b.sponsor_name_snapshot), ''), '—') AS "Sponsor Name",
                p.id AS "Patient ID",
                p.patient_id AS "Patient Unique Number",
                p.name AS "Patient Name",
                p.gender AS "Gender",
                CASE WHEN p.date_of_birth IS NOT NULL
                    THEN EXTRACT(YEAR FROM AGE(bi.service_date, p.date_of_birth))::integer
                    ELSE NULL END AS "Age",
                p.mobile AS "Mobile Number",
                bi.service_date AS "Service Date",
                bi.service_group_name AS "Service Group",
                bi.service_name AS "Service Name",
                bi.quantity AS "Quantity",
                bi.rate AS "Rate",
                bi.amount AS "Gross Amount",
                bi.discount_amount AS "Discount Amount",
                bi.gst_amount AS "GST Amount",
                bi.net_amount AS "Net Amount",
                b.department AS "Department",
                COALESCE(NULLIF(BTRIM(b.doctor_name), ''), '—') AS "Doctor Name",
                b.admission_date::date AS "Admission Date",
                b.discharge_date::date AS "Discharge Date",
                COALESCE(NULLIF(BTRIM(bi.room_type_name), ''), NULLIF(BTRIM(b.room_type_name), ''), rt.name, '—') AS "Room Type",
                COALESCE(NULLIF(BTRIM(bi.bed_number), ''), NULLIF(BTRIM(b.bed_number), ''), '—') AS "Bed Number",
                b.notes AS "Bill Notes"
            FROM public.bill_items bi
            JOIN public.bills b ON b.id = bi.bill_id
            JOIN public.patients p ON p.id = b.patient_db_id
            LEFT JOIN public.room_types rt ON rt.id = b.room_type_id
            WHERE b.bill_type = 'IP'
              AND bi.service_date >= %s::date
              AND bi.service_date <= %s::date
        """
        params: list[Any] = [date_from, date_to]
        query = f"SELECT * FROM ({base}) report_rows"
        if filters.get("search"):
            query += " WHERE CAST(row_to_json(report_rows) AS TEXT) ILIKE %s"
            params.append(f"%{filters['search']}%")
        query += ' ORDER BY "Service Date" ASC, "Bill Number" ASC, "Service Name" ASC'
        return query, params, None

    if report_type == "accrued-revenue":
        if not filters.get("year") or not filters.get("month"):
            raise ValueError("Year and month are required for the accrued revenue report.")
        year, month = int(filters["year"]), int(filters["month"])
        start_date = f"{year:04d}-{month:02d}-01"
        end_date = f"{year + 1:04d}-01-01" if month == 12 else f"{year:04d}-{month + 1:02d}-01"
        based_on = filters.get("based_on") or "patient-wise"
        if based_on not in {"patient-wise", "bill-wise"}:
            raise ValueError("Based on must be patient-wise or bill-wise.")
        item_filters = ["bi.service_date >= %s::date", "bi.service_date < %s::date"]
        params = [start_date, end_date]
        bill_type = filters.get("bill_type")
        if based_on == "bill-wise" and bill_type:
            item_filters.append("b.bill_type = %s")
            params.append(str(bill_type).upper())
        item_where = " AND ".join(item_filters)
        if based_on == "bill-wise":
            base = f"""
                WITH service_items AS (
                    SELECT bi.bill_id, bi.service_date, bi.service_name,
                        bi.amount, bi.discount_amount, bi.net_amount,
                        b.bill_number, b.bill_type, b.op_number, b.ip_number,
                        b.bill_date, b.admission_date, b.discharge_date,
                        p.patient_id, p.name AS patient_name, p.mobile, p.gender
                    FROM public.bill_items bi
                    JOIN public.bills b ON b.id = bi.bill_id
                    JOIN public.patients p ON p.id = b.patient_db_id
                    WHERE {item_where}
                )
                SELECT MAX(service_date) AS report_date,
                    bill_number AS "Bill Number", bill_type AS "Bill Type",
                    CASE WHEN bill_type = 'OP' THEN op_number ELSE ip_number END AS "OP/IP Number",
                    patient_id AS "Patient Number", patient_name AS "Patient Name",
                    mobile AS "Mobile", gender AS "Gender", bill_date AS "Bill Date",
                    admission_date::date AS "Admission Date", discharge_date::date AS "Discharge Date",
                    STRING_AGG(DISTINCT service_name || ' (' || TO_CHAR(service_date, 'DD Mon YYYY') || ')',
                        ', ' ORDER BY service_name || ' (' || TO_CHAR(service_date, 'DD Mon YYYY') || ')') AS "Services",
                    COUNT(*) AS "Service Item Count",
                    COALESCE(SUM(amount), 0) AS "Amount",
                    COALESCE(SUM(discount_amount), 0) AS "Discount",
                    COALESCE(SUM(net_amount), 0) AS "Net Amount"
                FROM service_items
                GROUP BY bill_id, bill_number, bill_type, op_number, ip_number,
                    patient_id, patient_name, mobile, gender, bill_date, admission_date, discharge_date
            """
        else:
            base = f"""
                WITH service_items AS (
                    SELECT bi.bill_id, bi.service_date, bi.amount, bi.discount_amount, bi.net_amount,
                        b.bill_number, p.id AS patient_db_id, p.patient_id,
                        p.name AS patient_name, p.mobile, p.gender
                    FROM public.bill_items bi
                    JOIN public.bills b ON b.id = bi.bill_id
                    JOIN public.patients p ON p.id = b.patient_db_id
                    WHERE {item_where}
                )
                SELECT MAX(service_date) AS report_date,
                    patient_id AS "Patient Number", patient_name AS "Patient Name",
                    mobile AS "Mobile", gender AS "Gender",
                    COUNT(DISTINCT bill_id) AS "Number of Bills",
                    STRING_AGG(DISTINCT bill_number, ', ' ORDER BY bill_number) AS "Bill Numbers",
                    COALESCE(SUM(amount), 0) AS "Amount",
                    COALESCE(SUM(discount_amount), 0) AS "Discount",
                    COALESCE(SUM(net_amount), 0) AS "Net Amount"
                FROM service_items
                GROUP BY patient_db_id, patient_id, patient_name, mobile, gender
            """
        query = f"SELECT * FROM ({base}) report_rows"
        if filters.get("search"):
            query += " WHERE CAST(row_to_json(report_rows) AS TEXT) ILIKE %s"
            params.append(f"%{filters['search']}%")
        query += " ORDER BY report_date DESC NULLS LAST"
        return query, params, based_on

    base = BASE_SELECTS[report_type]
    where = []
    params: list[Any] = []

    # Each Admission & Discharge row is a single event, so the common
    # report_date filter applies to that event's date in both JSON and CSV.
    if filters.get("date_from"):
        where.append("report_date::date >= %s::date")
        params.append(filters["date_from"])
    if filters.get("date_to"):
        where.append("report_date::date <= %s::date")
        params.append(filters["date_to"])
    if filters.get("bill_type") and report_type in {
        "cash-collection", "refunds", "outstanding", "service-wise", "service-group-wise"
    }:
        where.append('"Bill Type" = %s')
        params.append(filters["bill_type"].upper())
    if filters.get("payment_method") and report_type == "cash-collection":
        where.append('LOWER("Payment Method") = LOWER(%s)')
        params.append(filters["payment_method"])
    if filters.get("staff") and report_type == "cash-collection":
        where.append('"Collected By" ILIKE %s')
        params.append(f"%{filters['staff']}%")
    if filters.get("source") and report_type in {"patient-registration", "source-of-lead"}:
        where.append('"Source of Lead" = %s')
        params.append(filters["source"])
    if filters.get("service") and report_type == "service-wise":
        where.append('"Service Name" ILIKE %s')
        params.append(f"%{filters['service']}%")
    if filters.get("service_group") and report_type in {"service-wise", "service-group-wise"}:
        where.append('"Service Group" ILIKE %s')
        params.append(f"%{filters['service_group']}%")
    if filters.get("status") and report_type == "admission-discharge":
        if filters["status"] == "admission":
            where.append('"Event Type" = %s')
            params.append("Admission")
        elif filters["status"] == "discharge":
            where.append('"Event Type" = %s')
            params.append("Discharge")
    if filters.get("search"):
        where.append("CAST(row_to_json(report_rows) AS TEXT) ILIKE %s")
        params.append(f"%{filters['search']}%")
    query = f"SELECT * FROM ({base}) report_rows"
    if where:
        query += " WHERE " + " AND ".join(where)
    query += " ORDER BY report_date DESC NULLS LAST"
    return query, params, None


def _csv_cell(value, column_name: str):
    """Format database values consistently with the previous browser CSV export."""
    from datetime import date, datetime
    from decimal import Decimal

    if value is None:
        return ""
    is_date_only = column_name.strip().lower() in {"admission date", "discharge date"}
    if isinstance(value, datetime):
        if is_date_only:
            return value.strftime("%d/%m/%Y")
        # Database timestamps are displayed in India time in the previous export.
        if value.tzinfo is not None:
            value = value.astimezone(ZoneInfo("Asia/Kolkata"))
        return value.strftime("%d/%m/%Y, %I:%M %p")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def stream_report_csv(report_type: str, filters: dict[str, Any]):
    """Yield a UTF-8-BOM CSV using a server-side cursor and bounded fetch batches."""
    import csv
    import io
    from psycopg2.extras import RealDictCursor

    if report_type not in REPORTS:
        raise ValueError("Unknown report type.")
    query, params, accrued_based_on = _build_streaming_report_query(report_type, filters)

    connection = get_db_connection()
    try:
        # Accrued revenue previously appended a monthly total row; preserve that output.
        accrued_totals = None
        if report_type == "accrued-revenue":
            summary_query = f"""
                SELECT COALESCE(SUM("Amount"), 0) AS amount,
                       COALESCE(SUM("Discount"), 0) AS discount,
                       COALESCE(SUM("Net Amount"), 0) AS net_amount
                FROM ({query}) accrued_rows
            """
            with connection.cursor() as summary_cursor:
                summary_cursor.execute(summary_query, params)
                accrued_totals = summary_cursor.fetchone()

        cursor = connection.cursor(name=f"report_csv_{id(connection)}", cursor_factory=RealDictCursor)
        cursor.itersize = 1000
        cursor.execute(query, params)

        def csv_line(values):
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\r\n")
            writer.writerow(values)
            return buffer.getvalue()

        # Named/server-side cursors do not expose column metadata immediately
        # after DECLARE on all psycopg2/PostgreSQL combinations. Fetch the first
        # batch before reading cursor.description; this still keeps memory
        # bounded to one batch and also works when the query returns no rows.
        batch = cursor.fetchmany(1000)
        description = cursor.description
        if description is None:
            raise RuntimeError("The report query did not return a result set.")
        columns = [desc.name for desc in description if desc.name != "report_date"]

        yield "\ufeff" + csv_line(columns)
        while batch:
            for row in batch:
                yield csv_line([_csv_cell(row.get(column), column) for column in columns])
            batch = cursor.fetchmany(1000)

        if accrued_totals is not None and columns:
            total_row = {column: "" for column in columns}
            total_row[columns[0]] = "MONTHLY TOTAL"
            total_row["Amount"] = accrued_totals["amount"] or 0
            total_row["Discount"] = accrued_totals["discount"] or 0
            total_row["Net Amount"] = accrued_totals["net_amount"] or 0
            yield csv_line([_csv_cell(total_row.get(column), column) for column in columns])
        cursor.close()
    finally:
        connection.close()
