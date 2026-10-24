"""Track room/bed assignment periods for inpatient bills.

All functions accept the caller's cursor so history changes are committed or
rolled back together with the related bill operation.
"""

from datetime import date, datetime
from decimal import Decimal


def _normalise(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _assignment(bill):
    if not bill:
        return None
    return {
        "room_type_id": bill.get("room_type_id"),
        "room_type_name": bill.get("room_type_name"),
        "bed_number": bill.get("bed_number"),
    }


def _same_assignment(old, new):
    if old is None or new is None:
        return old is new
    return all(_normalise(old.get(key)) == _normalise(new.get(key)) for key in (
        "room_type_id", "room_type_name", "bed_number"
    ))


def create_initial_ip_room_assignment(cursor, *, bill, changed_by):
    """Create the first assignment when an IP bill is created."""
    if str(bill.get("bill_type") or "").upper() != "IP":
        return None
    if bill.get("room_type_id") is None and not bill.get("bed_number"):
        return None

    cursor.execute(
        """
        INSERT INTO public.ip_room_history (
            bill_id, room_type_id, room_type_name, bed_number,
            start_at, end_at, changed_by, change_reason
        )
        VALUES (%s, %s, %s, %s, clock_timestamp(), NULL, %s, %s)
        RETURNING id
        """,
        (
            bill["id"], bill.get("room_type_id"), bill.get("room_type_name"),
            bill.get("bed_number"), changed_by, "Initial IP room/bed assignment",
        ),
    )
    row = cursor.fetchone()
    return row["id"] if isinstance(row, dict) else row[0]


def sync_ip_room_assignment(cursor, *, old_bill, new_bill, changed_by):
    """Close the active assignment and open a new one when room/bed changes.

    The bill row is expected to be locked by the caller. If the bill is
    discharged, the active assignment is closed even when room/bed is unchanged.
    Existing bills that predate this feature are not assigned fabricated
    historical start times; their room timeline starts when this feature first
    observes a room/bed assignment.
    """
    if str(new_bill.get("bill_type") or "").upper() != "IP":
        return

    old_assignment = _assignment(old_bill)
    new_assignment = _assignment(new_bill)
    changed = not _same_assignment(old_assignment, new_assignment)
    newly_discharged = old_bill.get("discharge_date") is None and new_bill.get("discharge_date") is not None

    if not changed and not newly_discharged:
        # For an existing IP bill with no room history yet, establish the
        # current assignment now without pretending to know its earlier start.
        if new_bill.get("room_type_id") is not None or new_bill.get("bed_number"):
            cursor.execute(
                "SELECT id FROM public.ip_room_history WHERE bill_id = %s AND end_at IS NULL LIMIT 1",
                (new_bill["id"],),
            )
            if cursor.fetchone() is None:
                create_initial_ip_room_assignment(cursor, bill=new_bill, changed_by=changed_by)
        return

    cursor.execute(
        """
        UPDATE public.ip_room_history
        SET end_at = clock_timestamp()
        WHERE bill_id = %s AND end_at IS NULL
        RETURNING id
        """,
        (new_bill["id"],),
    )

    # A discharge closes the current period and does not open another one.
    if newly_discharged:
        return

    if new_assignment and (new_assignment.get("room_type_id") is not None or new_assignment.get("bed_number")):
        reason_parts = []
        if old_assignment and old_assignment.get("room_type_id") != new_assignment.get("room_type_id"):
            reason_parts.append("room type changed")
        if old_assignment and old_assignment.get("bed_number") != new_assignment.get("bed_number"):
            reason_parts.append("bed changed")
        reason = ", ".join(reason_parts) or "room/bed assignment changed"
        cursor.execute(
            """
            INSERT INTO public.ip_room_history (
                bill_id, room_type_id, room_type_name, bed_number,
                start_at, end_at, changed_by, change_reason
            )
            VALUES (%s, %s, %s, %s, clock_timestamp(), NULL, %s, %s)
            """,
            (
                new_bill["id"], new_assignment.get("room_type_id"),
                new_assignment.get("room_type_name"), new_assignment.get("bed_number"),
                changed_by, reason,
            ),
        )


def list_ip_room_history(bill_id: int):
    """Return room/bed assignments for a bill in chronological order."""
    from fastapi import HTTPException
    from psycopg2.extras import RealDictCursor
    from Utils.billing_helpers import get_db_connection, serialize_row

    connection = None
    try:
        connection = get_db_connection()
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT id, bill_type FROM public.bills WHERE id = %s",
                    (bill_id,),
                )
                bill = cursor.fetchone()
                if bill is None:
                    raise HTTPException(status_code=404, detail="Bill not found.")
                if str(bill.get("bill_type") or "").upper() != "IP":
                    raise HTTPException(status_code=422, detail="Room history is available only for IP bills.")
                cursor.execute(
                    """
                    SELECT
                        h.id,
                        h.bill_id,
                        h.room_type_id,
                        h.room_type_name,
                        h.bed_number,
                        h.start_at,
                        h.end_at,
                        h.changed_by,
                        COALESCE(NULLIF(TRIM(u.full_name), ''), 'Unknown User')
                            AS changed_by_name,
                        h.change_reason,
                        h.created_at
                    FROM public.ip_room_history AS h
                    LEFT JOIN public.users AS u
                        ON u.id = h.changed_by
                    WHERE h.bill_id = %s
                    ORDER BY h.start_at ASC, h.id ASC
                    """,
                    (bill_id,),
                )
                return [serialize_row(dict(row)) for row in cursor.fetchall()]
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to retrieve IP room history.")
    finally:
        if connection is not None:
            connection.close()
