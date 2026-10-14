# Router/BillingPriceResolution.py

import logging
import os

import psycopg2
from psycopg2.extras import RealDictCursor

from fastapi import APIRouter, HTTPException, Query


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/billing",
    tags=["Billing Price Resolution"],
)


# ---------------------------------------------------------
# DATABASE CONNECTION
# ---------------------------------------------------------

def get_db_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        logger.error("DATABASE_URL is not configured.")
        raise HTTPException(
            status_code=500,
            detail="Database configuration is missing.",
        )

    try:
        return psycopg2.connect(database_url)

    except psycopg2.Error:
        logger.exception("Failed to connect to the database.")
        raise HTTPException(
            status_code=500,
            detail="Unable to connect to the database.",
        )


# ---------------------------------------------------------
# 1. RESOLVE APPLICABLE SERVICE PRICE
# ---------------------------------------------------------

@router.get("/service-price")
def resolve_service_price(
    service_id: int = Query(gt=0),
    bill_type: str = Query(),
    room_type_id: int | None = Query(default=None, gt=0),
    doctor_id: int | None = Query(default=None, gt=0),
):
    """
    Resolve the applicable active price for a service.

    OP fixed price:
        ?service_id=1&bill_type=OP

    OP doctor-specific price:
        ?service_id=1&bill_type=OP&doctor_id=2

    IP fixed price:
        ?service_id=2&bill_type=IP

    IP room-specific price:
        ?service_id=2&bill_type=IP&room_type_id=3

    Pricing rules:
    - A doctor-specific price takes precedence when configured
      for the selected doctor.
    - A room-specific price takes precedence when configured
      for the selected room type.
    - If specific prices exist but no price matches the
      selected doctor or room, return an error.
    - If no specific prices are configured, use the fixed
      price if one exists.
    - If no applicable price exists, return an error.
    """

    bill_type = bill_type.strip().upper()

    if bill_type not in ("OP", "IP"):
        raise HTTPException(
            status_code=400,
            detail="bill_type must be OP or IP.",
        )

    if bill_type == "OP" and room_type_id is not None:
        raise HTTPException(
            status_code=400,
            detail="room_type_id is only applicable to IP billing.",
        )

    if bill_type == "IP" and doctor_id is not None:
        raise HTTPException(
            status_code=400,
            detail="doctor_id is only applicable to OP billing.",
        )

    connection = get_db_connection()

    try:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            # -------------------------------------------------
            # STEP 1: VALIDATE SERVICE
            # -------------------------------------------------

            cursor.execute(
                """
                SELECT
                    s.id,
                    s.name,
                    s.service_group_id,
                    sg.name AS service_group_name,
                    s.available_for_op,
                    s.available_for_ip,
                    s.is_active
                FROM services s
                INNER JOIN service_groups sg
                    ON sg.id = s.service_group_id
                WHERE s.id = %s
                """,
                (service_id,),
            )

            service = cursor.fetchone()

            if not service:
                raise HTTPException(
                    status_code=404,
                    detail="Service not found.",
                )

            if not service["is_active"]:
                raise HTTPException(
                    status_code=400,
                    detail="The selected service is inactive.",
                )

            if bill_type == "OP" and not service["available_for_op"]:
                raise HTTPException(
                    status_code=400,
                    detail="This service is not available for OP billing.",
                )

            if bill_type == "IP" and not service["available_for_ip"]:
                raise HTTPException(
                    status_code=400,
                    detail="This service is not available for IP billing.",
                )

            # -------------------------------------------------
            # STEP 2: VALIDATE ROOM TYPE OR DOCTOR
            # -------------------------------------------------

            selector_id = None
            selector_name = None

            if bill_type == "IP" and room_type_id is not None:

                cursor.execute(
                    """
                    SELECT id, name, is_active
                    FROM room_types
                    WHERE id = %s
                    """,
                    (room_type_id,),
                )

                room_type = cursor.fetchone()

                if not room_type:
                    raise HTTPException(
                        status_code=404,
                        detail="Room type not found.",
                    )

                if not room_type["is_active"]:
                    raise HTTPException(
                        status_code=400,
                        detail="The selected room type is inactive.",
                    )

                selector_id = room_type_id
                selector_name = room_type["name"]

            elif bill_type == "OP" and doctor_id is not None:

                cursor.execute(
                    """
                    SELECT id, name, is_active
                    FROM doctors
                    WHERE id = %s
                    """,
                    (doctor_id,),
                )

                doctor = cursor.fetchone()

                if not doctor:
                    raise HTTPException(
                        status_code=404,
                        detail="Doctor not found.",
                    )

                if not doctor["is_active"]:
                    raise HTTPException(
                        status_code=400,
                        detail="The selected doctor is inactive.",
                    )

                selector_id = doctor_id
                selector_name = doctor["name"]

            # -------------------------------------------------
            # STEP 3: RETRIEVE ACTIVE PRICES
            # -------------------------------------------------

            cursor.execute(
                """
                SELECT
                    sp.id AS price_id,
                    sp.service_id,
                    sp.bill_type,
                    sp.room_type_id,
                    rt.name AS room_type_name,
                    sp.doctor_id,
                    d.name AS doctor_name,
                    sp.amount,
                    sp.is_active
                FROM service_prices sp
                LEFT JOIN room_types rt
                    ON rt.id = sp.room_type_id
                LEFT JOIN doctors d
                    ON d.id = sp.doctor_id
                WHERE sp.service_id = %s
                  AND sp.bill_type = %s
                  AND sp.is_active = TRUE
                ORDER BY sp.id
                """,
                (service_id, bill_type),
            )

            prices = cursor.fetchall()

            if not prices:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "No active price is configured for this "
                        "service and billing type."
                    ),
                )

            # -------------------------------------------------
            # STEP 4: IDENTIFY FIXED AND SPECIFIC PRICES
            # -------------------------------------------------

            if bill_type == "OP":
                specific_prices = [
                    price
                    for price in prices
                    if price["doctor_id"] is not None
                ]

                fixed_prices = [
                    price
                    for price in prices
                    if price["doctor_id"] is None
                    and price["room_type_id"] is None
                ]

                selector_label = "doctor"

            else:
                specific_prices = [
                    price
                    for price in prices
                    if price["room_type_id"] is not None
                ]

                fixed_prices = [
                    price
                    for price in prices
                    if price["room_type_id"] is None
                    and price["doctor_id"] is None
                ]

                selector_label = "room type"

            # -------------------------------------------------
            # STEP 5: SELECT THE APPLICABLE PRICE
            # -------------------------------------------------

            selected_price = None

            if selector_id is not None:

                matching_prices = [
                    price
                    for price in specific_prices
                    if (
                        price["doctor_id"] == selector_id
                        if bill_type == "OP"
                        else price["room_type_id"] == selector_id
                    )
                ]

                if len(matching_prices) > 1:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Multiple active prices match this "
                            "pricing condition. Please correct "
                            "the pricing configuration."
                        ),
                    )

                if matching_prices:
                    selected_price = matching_prices[0]

                elif specific_prices:
                    # Do not silently substitute another doctor's
                    # or room type's price.
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"No active price is configured for "
                            f"the selected {selector_label}."
                        ),
                    )

                else:
                    # No specific pricing has been configured.
                    if len(fixed_prices) > 1:
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "Multiple active fixed prices exist "
                                "for this service. Please correct "
                                "the pricing configuration."
                            ),
                        )

                    if fixed_prices:
                        selected_price = fixed_prices[0]

            else:
                # No doctor or room type was supplied.
                if specific_prices and not fixed_prices:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"This service requires a {selector_label} "
                            "to determine the applicable price."
                        ),
                    )

                if len(fixed_prices) > 1:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Multiple active fixed prices exist "
                            "for this service. Please correct "
                            "the pricing configuration."
                        ),
                    )

                if fixed_prices:
                    selected_price = fixed_prices[0]

            # -------------------------------------------------
            # STEP 6: RETURN THE RESOLVED PRICE
            # -------------------------------------------------

            if selected_price is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "No applicable active price was found "
                        "for the selected service."
                    ),
                )

            return {
                "service_id": service["id"],
                "service_name": service["name"],
                "service_group_id": service["service_group_id"],
                "service_group_name": service["service_group_name"],
                "bill_type": bill_type,
                "price_id": selected_price["price_id"],
                "amount": selected_price["amount"],
                "pricing_type": (
                    "DOCTOR_SPECIFIC"
                    if selected_price["doctor_id"] is not None
                    else (
                        "ROOM_SPECIFIC"
                        if selected_price["room_type_id"] is not None
                        else "FIXED"
                    )
                ),
                "room_type_id": selected_price["room_type_id"],
                "room_type_name": selected_price["room_type_name"],
                "doctor_id": selected_price["doctor_id"],
                "doctor_name": selected_price["doctor_name"],
            }

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Failed to resolve service price.")
        raise HTTPException(
            status_code=500,
            detail="Unable to resolve the service price.",
        )

    finally:
        connection.close()
