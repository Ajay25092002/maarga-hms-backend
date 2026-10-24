"""Transactional audit logging for bill-related changes."""

import json
from collections import defaultdict, deque
from datetime import date, datetime
from decimal import Decimal

from fastapi import HTTPException
from psycopg2.extras import RealDictCursor

from Utils.billing_helpers import get_db_connection, serialize_row


def list_bill_audit_history(bill_id: int):
    """Return the audit timeline for one existing bill, newest first."""
    connection = None
    try:
        connection = get_db_connection()
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT id FROM public.bills WHERE id = %s", (bill_id,))
                if cursor.fetchone() is None:
                    raise HTTPException(status_code=404, detail="Bill not found.")
                cursor.execute(
                    """
                    SELECT
                        h.id, h.bill_id, h.action, h.entity_type, h.entity_id,
                        h.old_values, h.new_values, h.changed_by,
                        COALESCE(u.full_name, CASE WHEN h.changed_by IS NULL THEN 'System' END, 'Unknown user') AS changed_by_name,
                        h.changed_at, h.reason
                    FROM public.bill_audit_history h
                    LEFT JOIN public.users u ON u.id = h.changed_by
                    WHERE h.bill_id = %s
                    ORDER BY h.changed_at DESC, h.id DESC
                    """,
                    (bill_id,),
                )
                return [serialize_row(dict(row)) for row in cursor.fetchall()]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to retrieve bill audit history.")
    finally:
        if connection is not None:
            connection.close()


def _json_value(value):
    """Convert database values to JSON-safe values without losing readability."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(v) for v in value]
    return value


def write_bill_audit(
    cursor,
    *,
    bill_id: int,
    action: str,
    entity_type: str,
    changed_by: int | None,
    old_values=None,
    new_values=None,
    entity_id: int | None = None,
    reason: str | None = None,
):
    """Insert one audit event using the caller's current transaction."""
    cursor.execute(
        """
        INSERT INTO public.bill_audit_history (
            bill_id, action, entity_type, entity_id,
            old_values, new_values, changed_by, reason
        )
        VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)
        RETURNING id
        """,
        (
            bill_id,
            action,
            entity_type,
            entity_id,
            json.dumps(_json_value(old_values), ensure_ascii=False) if old_values is not None else None,
            json.dumps(_json_value(new_values), ensure_ascii=False) if new_values is not None else None,
            changed_by,
            reason,
        ),
    )
    row = cursor.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


def fetch_bill_items_for_audit(cursor, bill_id: int):
    cursor.execute(
        "SELECT * FROM public.bill_items WHERE bill_id = %s ORDER BY id",
        (bill_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


ITEM_DIFF_IGNORED_FIELDS = {
    # The bill is the source of truth for shared room/bed context. Recording
    # these once at bill level avoids one duplicate event per line item.
    "id", "bill_id", "created_at", "updated_at",
    "room_type_id", "room_type_name", "bed_number",
}


def _item_audit_payload(item):
    return {
        key: value
        for key, value in item.items()
        if key not in ITEM_DIFF_IGNORED_FIELDS
    }


def write_bill_item_diff(cursor, *, bill_id: int, old_items, new_items, changed_by: int):
    """Write meaningful added/removed/updated line-item audit events.

    The update endpoint replaces line-item rows. Pair rows by stable business
    identity (and preserve multiplicity for repeated services), while ignoring
    generated row timestamps and bill-level room/bed context. Actual billing
    fields such as service date, rate, quantity, discount and GST remain audited.
    """
    def signature(item):
        return (
            str(item.get("service_id") or ""),
            str(item.get("service_date") or ""),
            str(item.get("service_name") or "").strip().casefold(),
        )

    old_by_key = defaultdict(deque)
    new_by_key = defaultdict(deque)
    for item in old_items or []:
        old_by_key[signature(item)].append(item)
    for item in new_items or []:
        new_by_key[signature(item)].append(item)

    all_keys = list(dict.fromkeys([*old_by_key.keys(), *new_by_key.keys()]))
    for key in all_keys:
        old_queue = old_by_key[key]
        new_queue = new_by_key[key]
        paired = min(len(old_queue), len(new_queue))
        for _ in range(paired):
            old_item = old_queue.popleft()
            new_item = new_queue.popleft()
            old_payload = _item_audit_payload(old_item)
            new_payload = _item_audit_payload(new_item)
            if _json_value(old_payload) != _json_value(new_payload):
                write_bill_audit(
                    cursor,
                    bill_id=bill_id,
                    action="bill_item.updated",
                    entity_type="bill_item",
                    entity_id=new_item.get("id"),
                    changed_by=changed_by,
                    old_values=old_payload,
                    new_values=new_payload,
                )
        while old_queue:
            item = old_queue.popleft()
            write_bill_audit(
                cursor,
                bill_id=bill_id,
                action="bill_item.removed",
                entity_type="bill_item",
                entity_id=item.get("id"),
                changed_by=changed_by,
                old_values=_item_audit_payload(item),
            )
        while new_queue:
            item = new_queue.popleft()
            write_bill_audit(
                cursor,
                bill_id=bill_id,
                action="bill_item.added",
                entity_type="bill_item",
                entity_id=item.get("id"),
                changed_by=changed_by,
                new_values=_item_audit_payload(item),
            )


def search_bill_history(*, page: int = 1, page_size: int = 10, search: str | None = None, bill_type: str | None = None):
    """Search OP/IP bills for the bill-history screen with server-side pagination."""
    connection = None
    try:
        connection = get_db_connection()
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                clauses = []
                params = []
                if search and search.strip():
                    term = f"%{search.strip()}%"
                    clauses.append("""(
                        b.bill_number ILIKE %s OR
                        b.op_number ILIKE %s OR
                        b.ip_number ILIKE %s OR
                        p.patient_id ILIKE %s OR
                        p.name ILIKE %s
                    )""")
                    params.extend([term, term, term, term, term])
                if bill_type in ("OP", "IP"):
                    clauses.append("b.bill_type = %s")
                    params.append(bill_type)
                where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
                cursor.execute(
                    f"""
                    SELECT COUNT(*) AS total
                    FROM public.bills b
                    JOIN public.patients p ON p.id = b.patient_db_id
                    {where}
                    """,
                    tuple(params),
                )
                total = int(cursor.fetchone()["total"])
                offset = (page - 1) * page_size
                cursor.execute(
                    f"""
                    SELECT
                        b.id, b.bill_number, b.bill_type, b.op_number, b.ip_number,
                        b.bill_date, b.admission_date, b.discharge_date,
                        b.total_amount, b.updated_at, b.created_at,
                        p.patient_id, p.name AS patient_name, p.mobile
                    FROM public.bills b
                    JOIN public.patients p ON p.id = b.patient_db_id
                    {where}
                    ORDER BY COALESCE(b.updated_at, b.created_at) DESC, b.id DESC
                    LIMIT %s OFFSET %s
                    """,
                    tuple(params + [page_size, offset]),
                )
                return {
                    "items": [serialize_row(dict(row)) for row in cursor.fetchall()],
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                    "total_pages": (total + page_size - 1) // page_size,
                }
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to search bill history.")
    finally:
        if connection is not None:
            connection.close()
