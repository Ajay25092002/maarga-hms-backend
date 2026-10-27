# billing_service.py
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

import psycopg2
from fastapi import HTTPException
from psycopg2.extras import RealDictCursor

from Core.logger import get_logger
from Services.ip_room_history_service import (
    create_initial_ip_room_assignment,
    sync_ip_room_assignment,
)
from Schemas.billing_schema import CreateBillRequest
from Utils.billing_helpers import (
    ensure_payment_table,
    get_bill_items,
    get_db_connection,
    serialize_row,
)
from Services.bill_audit_service import (
    fetch_bill_items_for_audit,
    write_bill_audit,
    write_bill_item_diff,
)

logger = get_logger(__name__)

MONEY = Decimal("0.01")

FRONTEND_RATE_SERVICE_NAMES = {
    "RETRIEVAL CHARGES",
    "LAUNDRY CHARGES",
    "MISCELLANEOUS",
}

def money(value):
    return Decimal(str(value)).quantize(
        MONEY,
        rounding=ROUND_HALF_UP,
    )


def resolve_service_price(
    cursor,
    service_id: int,
    bill_type: str,
    doctor_id: int | None,
    room_type_id: int | None,
    allow_frontend_rate: bool = False,
):
    """
    Resolve an active service price from the database.

    OP:
        Use doctor-specific pricing when configured.
        Otherwise use the fixed OP price.

    IP:
        Use room-specific pricing when configured.
        Otherwise use the fixed IP price.

    Returns the selected service-price record ID along with
    the service and service-group information.
    """

    cursor.execute(
        """
        SELECT
            s.id,
            s.name AS service_name,
            s.service_group_id,
            s.is_active AS service_is_active,
            s.available_for_op,
            s.available_for_ip,
            s.requires_special_note,
            sg.name AS service_group_name,
            sg.is_active AS group_is_active
        FROM public.services s
        JOIN public.service_groups sg
            ON sg.id = s.service_group_id
        WHERE s.id = %s
        """,
        (service_id,),
    )

    service = cursor.fetchone()

    if service is None:
        raise HTTPException(
            status_code=422,
            detail=f"Service {service_id} does not exist.",
        )

    if not service["service_is_active"]:
        raise HTTPException(
            status_code=422,
            detail=f"Service '{service['service_name']}' is inactive.",
        )

    if not service["group_is_active"]:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Service group '{service['service_group_name']}' "
                "is inactive."
            ),
        )

    availability_field = (
        "available_for_op"
        if bill_type == "OP"
        else "available_for_ip"
    )

    if not service[availability_field]:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Service '{service['service_name']}' is not available "
                f"for {bill_type} billing."
            ),
        )

    # These services use a rate entered by an authorized frontend user.
    # Validate the service/group above, but do not require a service_prices row.
    service_name = str(service["service_name"]).strip().upper()
    if allow_frontend_rate and service_name in FRONTEND_RATE_SERVICE_NAMES:
        return {
            "service_id": service["id"],
            "service_group_id": service["service_group_id"],
            "service_price_id": None,
            "service_name": service["service_name"],
            "service_group_name": service["service_group_name"],
            "requires_special_note": service["requires_special_note"],
            "rate": None,
        }

    cursor.execute(
        """
        SELECT
            id,
            bill_type,
            doctor_id,
            room_type_id,
            amount
        FROM public.service_prices
        WHERE service_id = %s
          AND bill_type = %s
          AND is_active = TRUE
        ORDER BY id
        """,
        (service_id, bill_type),
    )

    prices = cursor.fetchall()

    if not prices:
        raise HTTPException(
            status_code=422,
            detail=(
                f"No active {bill_type} price is configured for "
                f"service '{service['service_name']}'."
            ),
        )

    selected_price = None

    if bill_type == "OP":
        if any(price["room_type_id"] is not None for price in prices):
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Invalid OP pricing configuration for "
                    f"'{service['service_name']}'."
                ),
            )

        specific_prices = [
            price
            for price in prices
            if price["doctor_id"] is not None
        ]

        if specific_prices:
            if doctor_id is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"A doctor must be selected for service "
                        f"'{service['service_name']}'."
                    ),
                )

            matches = [
                price
                for price in specific_prices
                if price["doctor_id"] == doctor_id
            ]

            if len(matches) != 1:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"No unique OP price is configured for service "
                        f"'{service['service_name']}' and the selected doctor."
                    ),
                )

            selected_price = matches[0]

        else:
            fixed_prices = [
                price
                for price in prices
                if price["doctor_id"] is None
            ]

            if len(fixed_prices) != 1:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"No unique fixed OP price is configured for "
                        f"'{service['service_name']}'."
                    ),
                )

            selected_price = fixed_prices[0]

    else:
        if any(price["doctor_id"] is not None for price in prices):
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Invalid IP pricing configuration for "
                    f"'{service['service_name']}'."
                ),
            )

        specific_prices = [
            price
            for price in prices
            if price["room_type_id"] is not None
        ]

        if specific_prices:
            if room_type_id is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"A room type must be selected for service "
                        f"'{service['service_name']}'."
                    ),
                )

            matches = [
                price
                for price in specific_prices
                if price["room_type_id"] == room_type_id
            ]

            if len(matches) != 1:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"No unique IP price is configured for service "
                        f"'{service['service_name']}' and the selected room type."
                    ),
                )

            selected_price = matches[0]

        else:
            fixed_prices = [
                price
                for price in prices
                if price["room_type_id"] is None
            ]

            if len(fixed_prices) != 1:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"No unique fixed IP price is configured for "
                        f"'{service['service_name']}'."
                    ),
                )

            selected_price = fixed_prices[0]

    return {
        "service_id": service["id"],
        "service_group_id": service["service_group_id"],
        "service_price_id": selected_price["id"],
        "service_name": service["service_name"],
        "service_group_name": service["service_group_name"],
        "requires_special_note": service["requires_special_note"],
        "rate": money(selected_price["amount"]),
    }


def validate_pricing_context(cursor, payload: CreateBillRequest):
    """
    Validate the selected doctor, psychologist and room type.

    Return database-backed names so bill records do not depend
    solely on names supplied by the frontend.
    """

    result = {
        "doctor_name": None,
        "psychologist_name": None,
        "room_type_name": None,
    }

    if payload.doctor_id is not None:
        cursor.execute(
            """
            SELECT id, name, is_active
            FROM public.doctors
            WHERE id = %s
            """,
            (payload.doctor_id,),
        )

        doctor = cursor.fetchone()

        if doctor is None or not doctor["is_active"]:
            raise HTTPException(
                status_code=422,
                detail="The selected doctor does not exist or is inactive.",
            )

        result["doctor_name"] = doctor["name"]

    if payload.psychologist_id is not None:
        cursor.execute(
            """
            SELECT id, name, is_active
            FROM public.doctors
            WHERE id = %s
            """,
            (payload.psychologist_id,),
        )

        psychologist = cursor.fetchone()

        if psychologist is None or not psychologist["is_active"]:
            raise HTTPException(
                status_code=422,
                detail=(
                    "The selected psychologist does not exist "
                    "or is inactive."
                ),
            )

        result["psychologist_name"] = psychologist["name"]

    if payload.room_type_id is not None:
        cursor.execute(
            """
            SELECT id, name, is_active
            FROM public.room_types
            WHERE id = %s
            """,
            (payload.room_type_id,),
        )

        room_type = cursor.fetchone()

        if room_type is None or not room_type["is_active"]:
            raise HTTPException(
                status_code=422,
                detail=(
                    "The selected room type does not exist or is inactive."
                ),
            )

        result["room_type_name"] = room_type["name"]

    return result


def resolve_item_room_context(cursor, bill_type: str, item, payload):
    """Resolve authoritative per-item room snapshot for IP services."""
    if bill_type != "IP":
        return {"room_type_id": None, "room_type_name": None, "bed_number": None}

    room_type_id = item.room_type_id or payload.room_type_id
    if room_type_id is None:
        raise HTTPException(status_code=422, detail="Room type is required for every IP service item.")

    cursor.execute(
        "SELECT id, name, is_active FROM public.room_types WHERE id = %s",
        (room_type_id,),
    )
    room = cursor.fetchone()
    if room is None or not room["is_active"]:
        raise HTTPException(status_code=422, detail=f"Room type {room_type_id} does not exist or is inactive.")

    item_bed = item.bed_number if item.bed_number is not None else payload.bed_number
    return {
        "room_type_id": room["id"],
        "room_type_name": room["name"],
        "bed_number": item_bed.strip() if isinstance(item_bed, str) and item_bed.strip() else None,
    }


def create_bill_service(payload: CreateBillRequest, created_by: int):
    connection = None

    try:
        connection = get_db_connection()

        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:

                # Retain the existing payment-table initialization helper.
                ensure_payment_table(cursor)

                # Resolve the patient using the unique patient_id.
                # The old registration_number column is no longer used.
                cursor.execute(
                    """
                    SELECT id, patient_id
                    FROM public.patients
                    WHERE patient_id = %s
                    """,
                    (payload.patient_id,),
                )

                patient = cursor.fetchone()

                if patient is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Patient not found.",
                    )

                # The new bills schema requires a room type for IP
                # and keeps room_type_id NULL for OP.
                if payload.bill_type == "OP" and payload.room_type_id is not None:
                    raise HTTPException(
                        status_code=422,
                        detail="Room type must not be provided for OP billing.",
                    )

                context = validate_pricing_context(cursor, payload)

                subtotal = Decimal("0.00")
                item_discount_total = Decimal("0.00")
                gst_total = Decimal("0.00")
                prepared_items = []

                for item in payload.items:
                    item_room = resolve_item_room_context(cursor, payload.bill_type, item, payload)
                    resolved = resolve_service_price(
                        cursor=cursor,
                        service_id=item.service_id,
                        bill_type=payload.bill_type,
                        doctor_id=(payload.doctor_id if payload.bill_type == "OP" else None),
                        room_type_id=item_room["room_type_id"],
                        allow_frontend_rate=True,
                    )

                    service_name = str(resolved["service_name"]).strip().upper()
                    special_note = (item.special_note or "").strip() or None
                    if payload.bill_type == "IP" and resolved.get("requires_special_note") and not special_note:
                        raise HTTPException(
                            status_code=422,
                            detail=f"A note is required for service '{resolved['service_name']}'.",
                        )
                    if payload.bill_type != "IP" or not resolved.get("requires_special_note"):
                        special_note = None

                    if service_name in FRONTEND_RATE_SERVICE_NAMES:
                        if item.rate is None:
                            raise HTTPException(
                                status_code=422,
                                detail=f"Rate is required for service '{service_name}'.",
                            )
                        rate = money(item.rate)

                        if rate < 0:
                            raise HTTPException(
                                status_code=422,
                                detail="Rate cannot be negative.",
                            )
                    else:
                        rate = resolved["rate"]

                    quantity = item.quantity
                    amount = money(rate * quantity)
                    item_discount = money(item.discount_amount)

                    # GST is permitted only for eligible IP room-rent items.
                    gst_eligible = (
                        payload.bill_type == "IP"
                        and resolved["service_group_name"].strip().upper()
                        == "ACCOMMODATION CHARGES"
                        and resolved["service_name"].strip().upper()
                        == "ROOM RENT"
                    )

                    requested_gst = money(item.gst_amount)

                    if not gst_eligible and requested_gst > Decimal("0.00"):
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                "GST is allowed only for IP Room Rent "
                                "under Accommodation Charges."
                            ),
                        )

                    gst_amount = requested_gst if gst_eligible else Decimal("0.00")


                    if item_discount > amount:
                        raise HTTPException(
                            status_code=422,
                            detail=(
                                f"Discount exceeds the amount for "
                                f"'{resolved['service_name']}'."
                            ),
                        )

                    net_amount = money(amount - item_discount)

                    subtotal += amount
                    item_discount_total += item_discount
                    gst_total += gst_amount

                    prepared_items.append(
                        {
                            "service_id": resolved["service_id"],
                            "service_group_id": resolved["service_group_id"],
                            "service_price_id": resolved["service_price_id"],
                            "service_name": resolved["service_name"],
                            "service_group_name": resolved["service_group_name"],
                            "service_date": item.service_date,
                            "special_note": special_note,
                            "room_type_id": item_room["room_type_id"],
                            "room_type_name": item_room["room_type_name"],
                            "bed_number": item_room["bed_number"],
                            "rate": rate,
                            "quantity": quantity,
                            "amount": amount,
                            "discount_amount": item_discount,
                            "net_amount": net_amount,
                            "gst_amount": gst_amount,
                        }
                    )

                
                subtotal = money(subtotal)
                item_discount_total = money(item_discount_total)
                gst_total = money(gst_total)
                bill_discount = money(payload.discount_amount)

                remaining = money(subtotal - item_discount_total)

                if bill_discount > remaining:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "The bill discount cannot exceed the amount "
                            "remaining after item discounts."
                        ),
                    )

                # GST is added after item and bill-level discounts.
                total_amount = money(
                    remaining - bill_discount + gst_total
                )


                # The database trigger generates bill_number, op_number
                # and ip_number. Do not generate or insert them here.
                cursor.execute(
                    """
                    INSERT INTO public.bills (
                        bill_type,
                        patient_db_id,
                        bill_date,
                        department,
                        room_type_id,
                        room_type_name,
                        doctor_id,
                        doctor_name,
                        psychologist_id,
                        psychologist_name,
                        subtotal,
                        discount_amount,
                        total_amount,
                        notes,
                        admission_date,
                        discharge_date,
                        bed_number,
                        rate_category,
                        created_by
                    )
                    VALUES (
                        %s, %s, CURRENT_TIMESTAMP, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING *
                    """,
                    (
                        payload.bill_type,
                        patient["id"],
                        payload.department,
                        (
                            payload.room_type_id
                            if payload.bill_type == "IP"
                            else None
                        ),
                        (
                            context["room_type_name"]
                            if payload.bill_type == "IP"
                            else None
                        ),
                        payload.doctor_id,
                        (
                            context["doctor_name"]
                            if payload.doctor_id is not None
                            else payload.doctor_name
                        ),
                        payload.psychologist_id,
                        (
                            context["psychologist_name"]
                            if payload.psychologist_id is not None
                            else payload.psychologist_name
                        ),
                        subtotal,
                        bill_discount,
                        total_amount,
                        payload.notes,
                        (
                            payload.admission_date
                            if payload.bill_type == "IP"
                            else None
                        ),
                        (
                            payload.discharge_date
                            if payload.bill_type == "IP"
                            else None
                        ),
                        (
                            payload.bed_number
                            if payload.bill_type == "IP"
                            else None
                        ),
                        (
                            payload.rate_category
                            if payload.bill_type == "IP"
                            else None
                        ),
                        created_by,
                    ),
                )

                bill = dict(cursor.fetchone())

                # Start the IP room/bed timeline within the bill creation transaction.
                create_initial_ip_room_assignment(
                    cursor, bill=bill, changed_by=created_by
                )

                # Save bill items using the updated database column names.
                for item in prepared_items:
                    cursor.execute(
                        """
                        
                        INSERT INTO public.bill_items (
                            bill_id,
                            service_id,
                            service_group_id,
                            service_price_id,
                            service_name,
                            service_group_name,
                            service_date,
                            special_note,
                            room_type_id,
                            room_type_name,
                            bed_number,
                            rate,
                            quantity,
                            amount,
                            discount_amount,
                            net_amount,
                            gst_amount
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )

                        """,
                        (
                            bill["id"],
                            item["service_id"],
                            item["service_group_id"],
                            item["service_price_id"],
                            item["service_name"],
                            item["service_group_name"],
                            item["service_date"],
                            item["special_note"],
                            item["room_type_id"],
                            item["room_type_name"],
                            item["bed_number"],
                            item["rate"],
                            item["quantity"],
                            item["amount"],
                            item["discount_amount"],
                            item["net_amount"],
                            item["gst_amount"],

                        ),
                    )

                created_items_for_audit = fetch_bill_items_for_audit(cursor, bill["id"])

                # Keep the bill-level creation snapshot and also create one
                # explicit audit event per initial line item. This makes the
                # initial services visible in the timeline just like items
                # added later, while keeping all audit writes in this same
                # transaction as bill creation.
                write_bill_audit(
                    cursor,
                    bill_id=bill["id"],
                    action="bill.created",
                    entity_type="bill",
                    entity_id=bill["id"],
                    changed_by=created_by,
                    new_values={
                        "bill": dict(bill),
                        "items": created_items_for_audit,
                    },
                )
                write_bill_item_diff(
                    cursor,
                    bill_id=bill["id"],
                    old_items=[],
                    new_items=created_items_for_audit,
                    changed_by=created_by,
                )

                result = serialize_row(bill)
                result["patient_id"] = patient["patient_id"]
                result["amount_paid"] = 0
                result["outstanding_amount"] = float(total_amount)
                result["payment_status"] = (
                    "Paid" if total_amount == 0 else "Unpaid"
                )
                result["items"] = get_bill_items(cursor, bill["id"])

                return result

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Database error while creating a bill.")
        raise HTTPException(
            status_code=500,
            detail="Unable to create the bill.",
        )

    except Exception:
        logger.exception("Unexpected error while creating a bill.")
        raise HTTPException(
            status_code=500,
            detail="An unexpected error occurred while creating the bill.",
        )

    finally:
        if connection is not None:
            connection.close()


def update_bill_service(bill_id: int, payload, updated_by: int):
    """Update an existing OP/IP bill and its items atomically.

    Payment and refund records are intentionally never modified here.
    Service prices are re-resolved on the server and totals recalculated.
    """
    connection = None
    try:
        connection = get_db_connection()
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                ensure_payment_table(cursor)
                cursor.execute("SELECT * FROM public.bills WHERE id = %s FOR UPDATE", (bill_id,))
                bill = cursor.fetchone()
                if not bill:
                    raise HTTPException(status_code=404, detail="Bill not found.")

                old_bill_for_audit = dict(bill)
                old_items_for_audit = fetch_bill_items_for_audit(cursor, bill_id)

                # Automated IP service rows are system-managed. They must remain
                # present with the same service, service date and quantity; the
                # discount and (Room Rent only) GST may still be edited.

                # disabled temporruily
                # automated_ip_services = {
                #     "room rent",
                #     "holistic therapies",
                #     "diet & food",
                #     "doctor consultation",
                #     "nursing charges",
                #     "psychiatric consultation/treatment",
                #     "psychologist counselling/treatment",
                #     "recreation actvities/therapy",
                #     "recreation activities/therapy",
                # }
                # if str(bill.get("bill_type") or "").upper() == "IP":
                #     required_rows = [
                #         old_item for old_item in old_items_for_audit
                #         if str(old_item.get("service_name") or "").strip().casefold()
                #         in automated_ip_services
                #     ]
                #     remaining_payload_items = list(payload.items)
                #     for old_item in required_rows:
                #         match_index = next(
                #             (
                #                 index for index, incoming in enumerate(remaining_payload_items)
                #                 if str(incoming.service_id) == str(old_item.get("service_id"))
                #                 and str(incoming.service_date)[:10] == str(old_item.get("service_date"))[:10]
                #                 and Decimal(str(incoming.quantity)) == Decimal(str(old_item.get("quantity") or 1))
                #             ),
                #             None,
                #         )
                #         if match_index is None:
                #             raise HTTPException(
                #                 status_code=409,
                #                 detail=(
                #                     f"Automated IP service '{old_item.get('service_name')}' "
                #                     "cannot be removed or have its service date or quantity changed."
                #                 ),
                #             )
                #         remaining_payload_items.pop(match_index)

                # Lock editing once an OP bill is fully paid, or an IP bill is discharged.
                if str(bill.get("bill_type") or "").upper() == "IP" and bill.get("discharge_date"):
                    raise HTTPException(status_code=409, detail="This IP bill cannot be edited after the discharge date has been recorded.")
                if str(bill.get("bill_type") or "").upper() == "OP":
                    cursor.execute(
                        "SELECT COALESCE(SUM(amount), 0) AS gross_paid FROM public.bill_payments WHERE bill_id = %s",
                        (bill_id,),
                    )
                    gross_paid_for_lock = Decimal(str(cursor.fetchone()["gross_paid"] or 0))
                    bill_total_for_lock = Decimal(str(bill.get("total_amount") or 0))
                    if bill_total_for_lock <= gross_paid_for_lock:
                        raise HTTPException(status_code=409, detail="This OP bill cannot be edited because it has been fully paid.")

                # A bill may be edited after partial/full payment, but its revised
                # total must not fall below the amount currently retained by the
                # hospital (gross payments less bill-level refunds). The bill row
                # is locked above, and payments/refunds lock the same bill row in
                # their transaction workflows, preventing concurrent bypasses.
                cursor.execute(
                    """
                    SELECT
                        COALESCE((SELECT SUM(amount) FROM public.bill_payments WHERE bill_id = %s), 0) AS gross_paid,
                        COALESCE((SELECT SUM(refund_amount) FROM public.payment_refunds WHERE bill_id = %s), 0) AS total_refunded
                    """,
                    (bill_id, bill_id),
                )
                financials = cursor.fetchone()
                gross_paid = Decimal(str(financials["gross_paid"] or 0))
                total_refunded = Decimal(str(financials["total_refunded"] or 0))
                net_paid = max(gross_paid - total_refunded, Decimal("0.00"))

                if bill["bill_type"] != payload.bill_type:
                    raise HTTPException(status_code=422, detail="Bill type cannot be changed.")
                if payload.bill_type == "OP" and payload.room_type_id is not None:
                    raise HTTPException(status_code=422, detail="Room type must not be provided for OP billing.")

                cursor.execute("SELECT id, patient_id FROM public.patients WHERE id = %s", (bill["patient_db_id"],))
                patient = cursor.fetchone()
                if not patient or patient["patient_id"] != payload.patient_id:
                    raise HTTPException(status_code=422, detail="The patient linked to an existing bill cannot be changed.")

                context = validate_pricing_context(cursor, payload)
                subtotal = Decimal("0.00")
                item_discount_total = Decimal("0.00")
                gst_total = Decimal("0.00")
                prepared_items = []
                for item in payload.items:
                    item_room = resolve_item_room_context(cursor, payload.bill_type, item, payload)
                    resolved = resolve_service_price(
                        cursor=cursor,
                        service_id=item.service_id,
                        bill_type=payload.bill_type,
                        doctor_id=payload.doctor_id if payload.bill_type == "OP" else None,
                        room_type_id=item_room["room_type_id"],
                        allow_frontend_rate=True,
                    )
                    service_name = str(resolved["service_name"]).strip().upper()
                    special_note = (item.special_note or "").strip() or None
                    if payload.bill_type == "IP" and resolved.get("requires_special_note") and not special_note:
                        raise HTTPException(
                            status_code=422,
                            detail=f"A note is required for service '{resolved['service_name']}'.",
                        )
                    if payload.bill_type != "IP" or not resolved.get("requires_special_note"):
                        special_note = None

                    if service_name in FRONTEND_RATE_SERVICE_NAMES:
                        if item.rate is None:
                            raise HTTPException(
                                status_code=422,
                                detail=f"Rate is required for service '{service_name}'.",
                            )
                        rate = money(item.rate)

                        if rate < 0:
                            raise HTTPException(
                                status_code=422,
                                detail="Rate cannot be negative.",
                            )
                    else:
                        rate = resolved["rate"]

                    quantity = item.quantity
                    amount = money(rate * quantity)
                    discount = money(item.discount_amount)
                    gst_eligible = (payload.bill_type == "IP" and
                                    resolved["service_group_name"].strip().upper() == "ACCOMMODATION CHARGES" and
                                    resolved["service_name"].strip().upper() == "ROOM RENT")
                    requested_gst = money(item.gst_amount)
                    if not gst_eligible and requested_gst > 0:
                        raise HTTPException(status_code=422, detail="GST is allowed only for IP Room Rent under Accommodation Charges.")
                    gst = requested_gst if gst_eligible else Decimal("0.00")
                    if discount > amount:
                        raise HTTPException(status_code=422, detail=f"Discount exceeds the amount for '{resolved['service_name']}'.")
                    net = money(amount - discount)
                    subtotal += amount
                    item_discount_total += discount
                    gst_total += gst
                    prepared_items.append({
                        "service_id": resolved["service_id"],
                        "service_group_id": resolved["service_group_id"],
                        "service_price_id": resolved["service_price_id"],
                        "service_name": resolved["service_name"],
                        "service_group_name": resolved["service_group_name"],
                        "service_date": item.service_date,
                        "special_note": special_note,
                        "room_type_id": item_room["room_type_id"],
                        "room_type_name": item_room["room_type_name"],
                        "bed_number": item_room["bed_number"],
                        "rate": rate, "quantity": quantity, "amount": amount,
                        "discount_amount": discount, "net_amount": net, "gst_amount": gst,
                    })
                subtotal = money(subtotal)
                item_discount_total = money(item_discount_total)
                gst_total = money(gst_total)
                bill_discount = money(payload.discount_amount)
                remaining = money(subtotal - item_discount_total)
                if bill_discount > remaining:
                    raise HTTPException(status_code=422, detail="The bill discount cannot exceed the amount remaining after item discounts.")
                total_amount = money(remaining - bill_discount + gst_total)
                # OP: Do not allow the revised bill total to fall below
                # the net amount already paid.
                # IP: Allow a lower revised total; the deposit balance will
                # be recalculated from the updated bill total.
                if payload.bill_type == "OP" and total_amount < net_paid:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"The revised OP bill total (₹{total_amount:,.2f}) cannot be less than "
                            f"the net amount already paid (₹{net_paid:,.2f})."
                        ),
                    )

                cursor.execute("""
                    UPDATE public.bills SET
                        -- The original bill creation timestamp is immutable.
                        department = %s, room_type_id = %s, room_type_name = %s,
                        doctor_id = %s, doctor_name = %s,
                        psychologist_id = %s, psychologist_name = %s, subtotal = %s,
                        discount_amount = %s, total_amount = %s, notes = %s,
                        admission_date = %s, discharge_date = %s, bed_number = %s,
                        rate_category = %s,
                        updated_by = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING *
                """, (
                    payload.department,
                    payload.room_type_id if payload.bill_type == "IP" else None,
                    context.get("room_type_name") if payload.bill_type == "IP" else None,
                    payload.doctor_id, context.get("doctor_name") if payload.doctor_id else payload.doctor_name,
                    payload.psychologist_id,
                    context.get("psychologist_name") if payload.psychologist_id else payload.psychologist_name,
                    subtotal, bill_discount, total_amount, payload.notes,
                    payload.admission_date if payload.bill_type == "IP" else None,
                    payload.discharge_date if payload.bill_type == "IP" else None,
                    payload.bed_number if payload.bill_type == "IP" else None,
                    payload.rate_category if payload.bill_type == "IP" else bill.get("rate_category"),
                    updated_by, bill_id,
                ))
                updated_bill = cursor.fetchone()
                # Record room type / bed assignment periods only when they change.
                # This shares the bill update transaction, so both stay consistent.
                sync_ip_room_assignment(
                    cursor,
                    old_bill=old_bill_for_audit,
                    new_bill=dict(updated_bill),
                    changed_by=updated_by,
                )
                # The transaction protects the bill and all its items as one unit.
                cursor.execute("DELETE FROM public.bill_items WHERE bill_id = %s", (bill_id,))
                for item in prepared_items:
                    cursor.execute("""
                        INSERT INTO public.bill_items (
                            bill_id, service_id, service_group_id, service_price_id,
                            service_name, service_group_name, service_date, special_note,
                            room_type_id, room_type_name, bed_number,
                            rate, quantity, amount, discount_amount, net_amount, gst_amount
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (bill_id, item["service_id"], item["service_group_id"], item["service_price_id"],
                          item["service_name"], item["service_group_name"], item["service_date"], item["special_note"],
                          item["room_type_id"], item["room_type_name"], item["bed_number"], item["rate"],
                          item["quantity"], item["amount"], item["discount_amount"], item["net_amount"], item["gst_amount"]))
                new_items_for_audit = fetch_bill_items_for_audit(cursor, bill_id)
                write_bill_audit(
                    cursor,
                    bill_id=bill_id,
                    action="bill.updated",
                    entity_type="bill",
                    entity_id=bill_id,
                    changed_by=updated_by,
                    old_values={
                        "bill": old_bill_for_audit,
                        "items": old_items_for_audit,
                    },
                    new_values={
                        "bill": dict(updated_bill),
                        "items": new_items_for_audit,
                    },
                )
                write_bill_item_diff(
                    cursor,
                    bill_id=bill_id,
                    old_items=old_items_for_audit,
                    new_items=new_items_for_audit,
                    changed_by=updated_by,
                )

                from Utils.billing_helpers import get_bill_items
                result = serialize_row(dict(updated_bill))
                result["items"] = get_bill_items(cursor, bill_id)
                # updated_by and updated_at are persisted on public.bills and
                # included in the returned bill row.
                return result
    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception("Database error while updating bill %s", bill_id)
        raise HTTPException(status_code=500, detail="Unable to update bill.")
    except Exception:
        logger.exception("Unexpected error while updating bill %s", bill_id)
        raise HTTPException(status_code=500, detail="An unexpected error occurred while updating the bill.")


NURSE_ALLOWED_SERVICE_GROUPS = {
    "clinical services",
    "medical equipment",
    "medical equipments",
    "procedure",
    "procedures",
    "psychiatry charges",
    "radiology charges",
    "pathology charges",
}


def add_nurse_ip_service(
    bill_id: int, service_id: int, quantity, service_date, updated_by: int,
    allow_all_service_groups: bool = False,
):
    """Append a service to an IP bill; enforce role-based group restrictions."""
    from decimal import Decimal
    from datetime import date
    connection = None
    try:
        connection = get_db_connection()
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("SELECT * FROM public.bills WHERE id = %s FOR UPDATE", (bill_id,))
                bill = cursor.fetchone()
                if not bill:
                    raise HTTPException(status_code=404, detail="Bill not found.")
                if str(bill["bill_type"]).upper() != "IP":
                    raise HTTPException(status_code=422, detail="Nurse services can only be added to IP bills.")
                if bill.get("discharge_date"):
                    raise HTTPException(status_code=409, detail="Services cannot be added after the IP discharge date has been recorded.")
                resolved = resolve_service_price(
                    cursor, service_id, "IP", None, bill.get("room_type_id")
                )
                service_group = str(resolved.get("service_group_name") or "").strip().lower()
                if (
                    not allow_all_service_groups
                    and service_group not in NURSE_ALLOWED_SERVICE_GROUPS
                ):
                    raise HTTPException(
                        status_code=403,
                        detail=(
                            "Nurses can add services only from the six permitted "
                            "service groups. Ask an administrator to update the role "
                            "permission if access to all IP service groups is required."
                        ),
                    )
                qty = Decimal(str(quantity))
                if qty <= 0:
                    raise HTTPException(status_code=422, detail="Quantity must be greater than zero.")
                if service_date and service_date > date.today():
                    raise HTTPException(status_code=422, detail="Service date cannot be in the future.")

                rate = money(resolved["rate"])
                amount = money(rate * qty)
                cursor.execute("""
                    INSERT INTO public.bill_items (
                        bill_id, service_id, service_group_id, service_price_id,
                        service_name, service_group_name, service_date,
                        room_type_id, room_type_name, bed_number,
                        rate, quantity, amount, discount_amount, net_amount, gst_amount
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,0)
                    RETURNING id
                """, (bill_id, resolved["service_id"], resolved["service_group_id"],
                      resolved["service_price_id"], resolved["service_name"],
                      resolved["service_group_name"], service_date, bill.get("room_type_id"),
                      bill.get("room_type_name"), bill.get("bed_number"), rate, qty, amount, amount))
                item_id = cursor.fetchone()["id"]
                cursor.execute("""
                    SELECT COALESCE(SUM(amount),0) AS subtotal,
                           COALESCE(SUM(discount_amount),0) AS item_discounts,
                           COALESCE(SUM(gst_amount),0) AS gst_total
                    FROM public.bill_items WHERE bill_id = %s
                """, (bill_id,))
                sums = cursor.fetchone()
                subtotal = money(sums["subtotal"])
                item_discounts = money(sums["item_discounts"])
                gst_total = money(sums["gst_total"])
                bill_discount = money(bill.get("discount_amount") or 0)
                total = money(subtotal - item_discounts - bill_discount + gst_total)
                cursor.execute("""
                    UPDATE public.bills SET subtotal=%s, total_amount=%s,
                        updated_by=%s, updated_at=CURRENT_TIMESTAMP
                    WHERE id=%s
                """, (subtotal, total, updated_by, bill_id))
                cursor.execute("SELECT * FROM public.bills WHERE id = %s", (bill_id,))
                updated_bill_for_audit = dict(cursor.fetchone())
                cursor.execute("SELECT * FROM public.bill_items WHERE id = %s", (item_id,))
                added_item_for_audit = dict(cursor.fetchone())
                write_bill_audit(
                    cursor,
                    bill_id=bill_id,
                    action="bill_item.added",
                    entity_type="bill_item",
                    entity_id=item_id,
                    changed_by=updated_by,
                    old_values=None,
                    new_values={"item": added_item_for_audit},
                )
                old_totals = {
                    "subtotal": str(money(bill.get("subtotal") or 0)),
                    "total_amount": str(money(bill.get("total_amount") or 0)),
                }
                new_totals = {
                    "subtotal": str(subtotal),
                    "total_amount": str(total),
                }
                if old_totals != new_totals:
                    write_bill_audit(
                        cursor,
                        bill_id=bill_id,
                        action="bill.totals_updated",
                        entity_type="bill",
                        entity_id=bill_id,
                        changed_by=updated_by,
                        old_values=old_totals,
                        new_values=new_totals,
                        reason="Bill totals recalculated after a service was added.",
                    )
                return {"success": True, "bill_id": bill_id, "item_id": item_id,
                        "service_name": resolved["service_name"], "total_amount": float(total)}
    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception("Database error adding nurse service to IP bill %s", bill_id)
        raise HTTPException(status_code=500, detail="Unable to add service to IP bill.")
    finally:
        if connection is not None:
            connection.close()
