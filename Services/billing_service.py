
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

import psycopg2
from fastapi import HTTPException
from psycopg2.extras import RealDictCursor

from Core.logger import get_logger
from Schemas.billing_schema import CreateBillRequest
from Utils.billing_helpers import (
    ensure_payment_table,
    get_bill_items,
    get_db_connection,
    serialize_row,
)

logger = get_logger(__name__)

MONEY = Decimal("0.01")


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


def create_bill_service(payload: CreateBillRequest):
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
                prepared_items = []

                for item in payload.items:
                    resolved = resolve_service_price(
                        cursor=cursor,
                        service_id=item.service_id,
                        bill_type=payload.bill_type,
                        doctor_id=(
                            payload.doctor_id
                            if payload.bill_type == "OP"
                            else None
                        ),
                        room_type_id=(
                            payload.room_type_id
                            if payload.bill_type == "IP"
                            else None
                        ),
                    )

                    rate = resolved["rate"]
                    quantity = item.quantity
                    amount = money(rate * quantity)
                    item_discount = money(item.discount_amount)

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

                    prepared_items.append(
                        {
                            "service_id": resolved["service_id"],
                            "service_group_id": resolved["service_group_id"],
                            "service_price_id": resolved["service_price_id"],
                            "service_name": resolved["service_name"],
                            "service_group_name": resolved["service_group_name"],
                            "service_date": item.service_date,
                            "rate": rate,
                            "quantity": quantity,
                            "amount": amount,
                            "discount_amount": item_discount,
                            "net_amount": net_amount,
                        }
                    )

                subtotal = money(subtotal)
                item_discount_total = money(item_discount_total)
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

                total_amount = money(remaining - bill_discount)

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
                        rate_category                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    RETURNING *
                    """,
                    (
                        payload.bill_type,
                        patient["id"],
                        payload.bill_date or datetime.now(),
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
                    ),
                )

                bill = dict(cursor.fetchone())

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
                            rate,
                            quantity,
                            amount,
                            discount_amount,
                            net_amount
                        )
                        VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s, %s, %s
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
                            item["rate"],
                            item["quantity"],
                            item["amount"],
                            item["discount_amount"],
                            item["net_amount"],
                        ),
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
