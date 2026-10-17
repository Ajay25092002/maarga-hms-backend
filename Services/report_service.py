"""Read-only reporting queries for OP/IP Billing."""
from datetime import date, datetime, time, timedelta
from typing import Any

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
}

BASE_SELECTS = {
    "patient-registration": '''SELECT p.registered_at AS report_date, p.patient_id AS "Patient Number", p.name AS "Patient Name",
        p.mobile AS "Mobile", p.gender AS "Gender", p.date_of_birth AS "Date of Birth",
        COALESCE(NULLIF(p.source_of_lead,''),'Not Specified') AS "Source of Lead", p.registered_at AS "Registration Date"
        FROM public.patients p''',
    "op-billing": '''SELECT b.bill_date AS report_date, b.bill_number AS "Bill Number", b.op_number AS "OP Number",
        p.patient_id AS "Patient Number", p.name AS "Patient Name", b.bill_date AS "Bill Date",
        b.total_amount AS "Bill Amount", COALESCE(pay.paid,0) AS "Amount Paid",
        GREATEST(b.total_amount-COALESCE(pay.paid,0),0) AS "Outstanding Amount",
        CASE WHEN COALESCE(pay.paid,0)<=0 THEN 'Unpaid' WHEN COALESCE(pay.paid,0)<b.total_amount THEN 'Partially Paid' ELSE 'Paid' END AS "Payment Status",
        COALESCE(u.full_name,'—') AS "Billed By"
        FROM public.bills b JOIN public.patients p ON p.id=b.patient_db_id
        LEFT JOIN public.users u ON u.id=b.created_by
        LEFT JOIN (SELECT bill_id,SUM(amount) paid FROM public.bill_payments GROUP BY bill_id) pay ON pay.bill_id=b.id
        WHERE b.bill_type='OP' ''',
    "ip-billing": '''SELECT b.bill_date AS report_date, b.bill_number AS "Bill Number", b.ip_number AS "IP Number",
        p.patient_id AS "Patient Number", p.name AS "Patient Name", b.bill_date AS "Bill Date",
        b.admission_date AS "Admission Date", b.discharge_date AS "Discharge Date",
        b.total_amount AS "Bill Amount", COALESCE(pay.paid,0) AS "Amount Paid",
        GREATEST(b.total_amount-COALESCE(pay.paid,0),0) AS "Outstanding Amount",
        CASE WHEN COALESCE(pay.paid,0)<=0 THEN 'Unpaid' WHEN COALESCE(pay.paid,0)<b.total_amount THEN 'Partially Paid' ELSE 'Paid' END AS "Payment Status",
        COALESCE(u.full_name,'—') AS "Billed By"
        FROM public.bills b JOIN public.patients p ON p.id=b.patient_db_id
        LEFT JOIN public.users u ON u.id=b.created_by
        LEFT JOIN (SELECT bill_id,SUM(amount) paid FROM public.bill_payments GROUP BY bill_id) pay ON pay.bill_id=b.id
        WHERE b.bill_type='IP' ''',
    "cash-collection": '''SELECT bp.payment_date AS report_date, bp.receipt_number AS "Receipt Number", b.bill_number AS "Bill Number",
        CASE WHEN b.bill_type='OP' THEN b.op_number ELSE b.ip_number END AS "OP/IP Number",
        b.bill_type AS "Bill Type", p.name AS "Patient Name", bp.payment_date AS "Payment Date",
        bp.payment_method AS "Payment Method", bp.amount AS "Payment Amount", COALESCE(u.full_name,'—') AS "Collected By"
        FROM public.bill_payments bp JOIN public.bills b ON b.id=bp.bill_id JOIN public.patients p ON p.id=b.patient_db_id
        LEFT JOIN public.users u ON u.id=bp.created_by ''',
    "refunds": '''SELECT pr.created_at AS report_date, pr.refund_number AS "Refund Number", b.bill_number AS "Bill Number",
        CASE WHEN b.bill_type='OP' THEN b.op_number ELSE b.ip_number END AS "OP/IP Number", b.bill_type AS "Bill Type",
        p.name AS "Patient Name", pr.created_at AS "Refund Date", pr.refund_amount AS "Refund Amount",
        pr.refund_method AS "Refund Method", pr.reason AS "Reason", COALESCE(u.full_name,'—') AS "Refunded By"
        FROM public.payment_refunds pr JOIN public.bills b ON b.id=pr.bill_id JOIN public.patients p ON p.id=b.patient_db_id
        LEFT JOIN public.users u ON u.id=pr.created_by ''',
    "outstanding": '''SELECT b.bill_date AS report_date, b.bill_number AS "Bill Number", b.bill_type AS "Bill Type",
        CASE WHEN b.bill_type='OP' THEN b.op_number ELSE b.ip_number END AS "OP/IP Number",
        p.patient_id AS "Patient Number", p.name AS "Patient Name", b.bill_date AS "Bill Date",
        b.total_amount AS "Bill Amount", COALESCE(pay.paid,0) AS "Amount Paid",
        GREATEST(b.total_amount-COALESCE(pay.paid,0),0) AS "Outstanding Amount",
        CASE WHEN COALESCE(pay.paid,0)<=0 THEN 'Unpaid' ELSE 'Partially Paid' END AS "Payment Status", pay.last_payment AS "Last Payment Date"
        FROM public.bills b JOIN public.patients p ON p.id=b.patient_db_id
        LEFT JOIN (SELECT bill_id,SUM(amount) paid,MAX(payment_date) last_payment FROM public.bill_payments GROUP BY bill_id) pay ON pay.bill_id=b.id
        WHERE COALESCE(pay.paid,0)<b.total_amount ''',
    "service-wise": '''SELECT b.bill_date::date AS report_date, b.bill_type AS "Bill Type", bi.service_name AS "Service Name", bi.service_group_name AS "Service Group",
        SUM(bi.quantity) AS "Quantity", SUM(bi.net_amount) AS "Total Billed Amount", COUNT(DISTINCT b.id) AS "Number of Bills"
        FROM public.bill_items bi JOIN public.bills b ON b.id=bi.bill_id
        GROUP BY b.bill_date::date, b.bill_type, bi.service_name, bi.service_group_name ''',
    "service-group-wise": '''SELECT b.bill_date::date AS report_date, b.bill_type AS "Bill Type", bi.service_group_name AS "Service Group",
        COUNT(DISTINCT bi.service_name) AS "Number of Services", SUM(bi.quantity) AS "Total Quantity",
        COUNT(DISTINCT b.id) AS "Number of Bills", SUM(bi.net_amount) AS "Total Billed Amount"
        FROM public.bill_items bi JOIN public.bills b ON b.id=bi.bill_id
        GROUP BY b.bill_date::date, b.bill_type, bi.service_group_name ''',
    "admission-discharge": '''SELECT COALESCE(b.admission_date,b.discharge_date,b.bill_date) AS report_date, b.bill_number AS "Bill Number",
        b.ip_number AS "IP Number", p.patient_id AS "Patient Number", p.name AS "Patient Name",
        b.admission_date AS "Admission Date", b.discharge_date AS "Discharge Date",
        CASE WHEN b.discharge_date IS NULL THEN 'Currently Admitted' ELSE 'Discharged' END AS "Status",
        CASE WHEN b.admission_date IS NOT NULL AND b.discharge_date IS NOT NULL THEN GREATEST(0, b.discharge_date::date-b.admission_date::date) ELSE NULL END AS "Length of Stay (Days)"
        FROM public.bills b JOIN public.patients p ON p.id=b.patient_db_id WHERE b.bill_type='IP' ''',
    "source-of-lead": '''SELECT p.registered_at AS report_date, COALESCE(NULLIF(p.source_of_lead,''),'Not Specified') AS "Source of Lead",
        COUNT(*) OVER (PARTITION BY COALESCE(NULLIF(p.source_of_lead,''),'Not Specified')) AS "Patient Count",
        ROUND(COUNT(*) OVER (PARTITION BY COALESCE(NULLIF(p.source_of_lead,''),'Not Specified')) * 100.0 / NULLIF(COUNT(*) OVER (),0), 2) AS "Percentage",
        p.registered_at AS "Registration Date", p.patient_id AS "Patient Number", p.name AS "Patient Name", p.mobile AS "Mobile"
        FROM public.patients p '''
}

def fetch_report(report_type: str, filters: dict[str, Any], page: int, page_size: int, export_all: bool = False):
    if report_type not in REPORTS:
        raise ValueError("Unknown report type.")
    base = BASE_SELECTS[report_type]
    where = []
    params: list[Any] = []
    if filters.get("date_from"):
        where.append("report_date >= %s")
        params.append(datetime.combine(date.fromisoformat(filters["date_from"]), time.min))
    if filters.get("date_to"):
        where.append("report_date < %s")
        params.append(datetime.combine(date.fromisoformat(filters["date_to"]), time.min) + timedelta(days=1))
    if filters.get("bill_type") and report_type in {"cash-collection", "refunds", "outstanding", "service-wise", "service-group-wise"}:
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
        if filters["status"] == "admitted":
            where.append('"Status" = %s')
            params.append("Currently Admitted")
        elif filters["status"] == "discharged":
            where.append('"Status" = %s')
            params.append("Discharged")
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
            cursor.execute(f"SELECT COUNT(*) AS total FROM ({query}) count_rows", params)
            total = cursor.fetchone()["total"]
            if not export_all:
                query += " LIMIT %s OFFSET %s"
                params.extend([page_size, (page - 1) * page_size])
            cursor.execute(query, params)
            items = [serialize_row(row) for row in cursor.fetchall()]
            numeric_keys = ["Bill Amount", "Amount Paid", "Outstanding Amount", "Payment Amount", "Refund Amount", "Total Billed Amount"]
            totals = {}
            for key in numeric_keys:
                if items and key in items[0]:
                    totals[key] = round(sum(float(item.get(key) or 0) for item in items), 2)
            return {"report_type": report_type, "report_name": REPORTS[report_type], "items": items, "total": total, "page": page, "page_size": total if export_all else page_size, "totals": totals}
    finally:
        connection.close()
