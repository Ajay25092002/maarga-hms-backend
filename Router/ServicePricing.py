
import logging
import os
import math
from decimal import Decimal

import psycopg2
from psycopg2 import errors
from psycopg2.extras import RealDictCursor

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/service-pricing",
    tags=["Service Pricing"],
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
        return psycopg2.connect(database_url, options="-c timezone=Asia/Kolkata")
    except psycopg2.Error:
        logger.exception("Failed to connect to the database.")
        raise HTTPException(
            status_code=500,
            detail="Unable to connect to the database.",
        )


# ---------------------------------------------------------
# REQUEST SCHEMAS
# ---------------------------------------------------------

class ServicePriceCreate(BaseModel):
    service_id: int = Field(gt=0)
    bill_type: str
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    room_type_id: int | None = Field(default=None, gt=0)
    doctor_id: int | None = Field(default=None, gt=0)


class ServicePriceUpdate(BaseModel):
    service_id: int = Field(gt=0)
    bill_type: str
    amount: Decimal = Field(ge=0, max_digits=12, decimal_places=2)
    room_type_id: int | None = Field(default=None, gt=0)
    doctor_id: int | None = Field(default=None, gt=0)


class ServicePriceStatusUpdate(BaseModel):
    is_active: bool


# ---------------------------------------------------------
# RESPONSE HELPERS
# ---------------------------------------------------------

SERVICE_PRICE_SELECT = """
    SELECT
        sp.id,
        sp.service_id,
        s.name AS service_name,
        s.service_group_id,
        sg.name AS service_group_name,
        sp.bill_type,
        sp.room_type_id,
        rt.name AS room_type_name,
        sp.doctor_id,
        d.name AS doctor_name,
        sp.amount,
        sp.is_active,
        sp.created_at,
        sp.updated_at
    FROM service_prices sp
    INNER JOIN services s ON s.id = sp.service_id
    INNER JOIN service_groups sg ON sg.id = s.service_group_id
    LEFT JOIN room_types rt ON rt.id = sp.room_type_id
    LEFT JOIN doctors d ON d.id = sp.doctor_id
"""


def pagination_response(items, total, page, page_size):
    total_pages = math.ceil(total / page_size) if total else 0

    return {
        "items": items,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_previous": page > 1 and total > 0,
            "has_next": page < total_pages,
        },
    }


# ---------------------------------------------------------
# VALIDATION HELPERS
# ---------------------------------------------------------

def validate_price_data(
    service_id: int,
    bill_type: str,
    amount: Decimal,
    room_type_id: int | None,
    doctor_id: int | None,
):
    bill_type = bill_type.strip().upper()

    if bill_type not in ("OP", "IP"):
        raise HTTPException(
            status_code=400,
            detail="bill_type must be OP or IP.",
        )

    if amount < 0:
        raise HTTPException(
            status_code=400,
            detail="Price cannot be negative.",
        )

    if room_type_id is not None and doctor_id is not None:
        raise HTTPException(
            status_code=400,
            detail="A price cannot be both room-specific and doctor-specific.",
        )

    if bill_type == "OP" and room_type_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Room-specific prices are only supported for IP billing.",
        )

    if bill_type == "IP" and doctor_id is not None:
        raise HTTPException(
            status_code=400,
            detail="Doctor-specific prices are only supported for OP billing.",
        )

    connection = get_db_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT id, name, available_for_op, available_for_ip, is_active
                FROM services
                WHERE id = %s
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
                    detail="Cannot configure pricing for an inactive service.",
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

            if room_type_id is not None:
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
                        detail="Cannot use an inactive room type.",
                    )

            if doctor_id is not None:
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
                        detail="Cannot use an inactive doctor.",
                    )

        return bill_type

    finally:
        connection.close()




# ---------------------------------------------------------
# 1. GET SERVICE PRICES
# Search, filtering and pagination
# ---------------------------------------------------------

@router.get("/service-prices")
def get_service_prices(
    search: str | None = Query(default=None, max_length=150),
    service_id: int | None = Query(default=None, gt=0),
    service_group_id: int | None = Query(default=None, gt=0),
    bill_type: str | None = Query(default=None),
    room_type_id: int | None = Query(default=None, gt=0),
    doctor_id: int | None = Query(default=None, gt=0),
    is_active: bool | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
):
    if bill_type is not None:
        bill_type = bill_type.strip().upper()

        if bill_type not in ("OP", "IP"):
            raise HTTPException(
                status_code=400,
                detail="bill_type must be OP or IP.",
            )

    # Preserve compatibility with older callers that expect a list.
    use_pagination = page is not None or page_size is not None
    current_page = page or 1
    current_page_size = page_size or 10

    conditions = ["1 = 1"]
    params = []

    # Search by service, service group, room, doctor,
    # bill type, or amount.
    if search and search.strip():
        conditions.append(
            """
            (
                s.name ILIKE %s
                OR sg.name ILIKE %s
                OR COALESCE(rt.name, '') ILIKE %s
                OR COALESCE(d.name, '') ILIKE %s
                OR sp.bill_type ILIKE %s
                OR CAST(sp.amount AS TEXT) ILIKE %s
            )
            """
        )

        term = f"%{search.strip()}%"
        params.extend([term, term, term, term, term, term])

    # Filter by service.
    if service_id is not None:
        conditions.append("sp.service_id = %s")
        params.append(service_id)

    # Filter by service group.
    if service_group_id is not None:
        conditions.append("s.service_group_id = %s")
        params.append(service_group_id)

    # Filter by OP/IP billing type.
    if bill_type is not None:
        conditions.append("sp.bill_type = %s")
        params.append(bill_type)

    # Filter by room type.
    if room_type_id is not None:
        conditions.append("sp.room_type_id = %s")
        params.append(room_type_id)

    # Filter by doctor.
    if doctor_id is not None:
        conditions.append("sp.doctor_id = %s")
        params.append(doctor_id)

    # Filter by active/inactive status.
    if is_active is not None:
        conditions.append("sp.is_active = %s")
        params.append(is_active)

    where_clause = " WHERE " + " AND ".join(conditions)

    connection = get_db_connection()

    try:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            # Count only records matching every selected filter.
            count_query = """
                SELECT COUNT(*) AS total
                FROM service_prices sp
                INNER JOIN services s
                    ON s.id = sp.service_id
                INNER JOIN service_groups sg
                    ON sg.id = s.service_group_id
                LEFT JOIN room_types rt
                    ON rt.id = sp.room_type_id
                LEFT JOIN doctors d
                    ON d.id = sp.doctor_id
            """ + where_clause

            cursor.execute(count_query, params)
            total = cursor.fetchone()["total"]

            # Retrieve records using the same filters.
            query = (
                SERVICE_PRICE_SELECT
                + where_clause
                + """
                    ORDER BY
                        sg.name,
                        s.name,
                        sp.bill_type,
                        rt.name NULLS FIRST,
                        d.name NULLS FIRST,
                        sp.id
                """
            )

            query_params = list(params)

            # Apply pagination when requested.
            if use_pagination:
                query += " LIMIT %s OFFSET %s"
                query_params.extend(
                    [
                        current_page_size,
                        (current_page - 1) * current_page_size,
                    ]
                )

            cursor.execute(query, query_params)
            items = cursor.fetchall()

            # Keep the original list response for non-paginated callers.
            if not use_pagination:
                return items

            return pagination_response(
                items,
                total,
                current_page,
                current_page_size,
            )

    except psycopg2.Error:
        logger.exception("Failed to retrieve service prices.")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve service prices.",
        )

    finally:
        connection.close()

# ---------------------------------------------------------
# 2. GET SERVICE PRICE BY ID
# ---------------------------------------------------------

@router.get("/service-prices/{price_id}")
def get_service_price(price_id: int = Path(gt=0)):
    connection = get_db_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                SERVICE_PRICE_SELECT + " WHERE sp.id = %s",
                (price_id,),
            )
            price = cursor.fetchone()

            if not price:
                raise HTTPException(
                    status_code=404,
                    detail="Service price not found.",
                )

            return price

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception("Failed to retrieve service price.")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve the service price.",
        )
    finally:
        connection.close()


# ---------------------------------------------------------
# 3. GET ALL PRICES FOR A SERVICE
# ---------------------------------------------------------

@router.get("/services/{service_id}/prices")
def get_prices_for_service(
    service_id: int = Path(gt=0),
    is_active: bool | None = Query(default=None),
    search: str | None = Query(default=None, max_length=150),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
):
    use_pagination = page is not None or page_size is not None
    current_page = page or 1
    current_page_size = page_size or 10

    connection = get_db_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                "SELECT id FROM services WHERE id = %s",
                (service_id,),
            )
            if not cursor.fetchone():
                raise HTTPException(
                    status_code=404,
                    detail="Service not found.",
                )

            conditions = ["sp.service_id = %s"]
            params = [service_id]

            if is_active is not None:
                conditions.append("sp.is_active = %s")
                params.append(is_active)

            if search and search.strip():
                term = f"%{search.strip()}%"
                conditions.append(
                    """
                    (
                        COALESCE(rt.name, '') ILIKE %s
                        OR COALESCE(d.name, '') ILIKE %s
                        OR sp.bill_type ILIKE %s
                        OR CAST(sp.amount AS TEXT) ILIKE %s
                    )
                    """
                )
                params.extend([term, term, term, term])

            where_clause = " WHERE " + " AND ".join(conditions)

            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM service_prices sp
                LEFT JOIN room_types rt ON rt.id = sp.room_type_id
                LEFT JOIN doctors d ON d.id = sp.doctor_id
                """
                + where_clause,
                params,
            )
            total = cursor.fetchone()["total"]

            query = (
                SERVICE_PRICE_SELECT
                + where_clause
                + """
                    ORDER BY
                        sp.bill_type,
                        rt.name NULLS FIRST,
                        d.name NULLS FIRST,
                        sp.id
                """
            )
            query_params = list(params)

            if use_pagination:
                query += " LIMIT %s OFFSET %s"
                query_params.extend(
                    [
                        current_page_size,
                        (current_page - 1) * current_page_size,
                    ]
                )

            cursor.execute(query, query_params)
            items = cursor.fetchall()

            if not use_pagination:
                return items

            return pagination_response(
                items,
                total,
                current_page,
                current_page_size,
            )

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception("Failed to retrieve prices for service.")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve prices for this service.",
        )
    finally:
        connection.close()


# ---------------------------------------------------------
# 4. CREATE SERVICE PRICE
# ---------------------------------------------------------

@router.post("/service-prices", status_code=201)
def create_service_price(payload: ServicePriceCreate):
    bill_type = validate_price_data(
        service_id=payload.service_id,
        bill_type=payload.bill_type,
        amount=payload.amount,
        room_type_id=payload.room_type_id,
        doctor_id=payload.doctor_id,
    )

    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM service_prices
                    WHERE service_id = %s
                      AND bill_type = %s
                      AND room_type_id IS NOT DISTINCT FROM %s
                      AND doctor_id IS NOT DISTINCT FROM %s
                    """,
                    (
                        payload.service_id,
                        bill_type,
                        payload.room_type_id,
                        payload.doctor_id,
                    ),
                )

                if cursor.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A price already exists for this service "
                            "and pricing condition. Update the existing price."
                        ),
                    )

                cursor.execute(
                    """
                    INSERT INTO service_prices (
                        service_id,
                        bill_type,
                        room_type_id,
                        doctor_id,
                        amount,
                        is_active
                    )
                    VALUES (%s, %s, %s, %s, %s, TRUE)
                    RETURNING
                        id,
                        service_id,
                        bill_type,
                        room_type_id,
                        doctor_id,
                        amount,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (
                        payload.service_id,
                        bill_type,
                        payload.room_type_id,
                        payload.doctor_id,
                        payload.amount,
                    ),
                )

                return cursor.fetchone()

    except HTTPException:
        raise
    except errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="This pricing condition already exists.",
        )
    except psycopg2.Error:
        logger.exception("Failed to create service price.")
        raise HTTPException(
            status_code=500,
            detail="Unable to create the service price.",
        )
    finally:
        connection.close()


# ---------------------------------------------------------
# 5. UPDATE SERVICE PRICE
# ---------------------------------------------------------

@router.put("/service-prices/{price_id}")
def update_service_price(
    payload: ServicePriceUpdate,
    price_id: int = Path(gt=0),
):
    bill_type = validate_price_data(
        service_id=payload.service_id,
        bill_type=payload.bill_type,
        amount=payload.amount,
        room_type_id=payload.room_type_id,
        doctor_id=payload.doctor_id,
    )

    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT id FROM service_prices WHERE id = %s",
                    (price_id,),
                )
                if not cursor.fetchone():
                    raise HTTPException(
                        status_code=404,
                        detail="Service price not found.",
                    )

                cursor.execute(
                    """
                    SELECT id
                    FROM service_prices
                    WHERE service_id = %s
                      AND bill_type = %s
                      AND room_type_id IS NOT DISTINCT FROM %s
                      AND doctor_id IS NOT DISTINCT FROM %s
                      AND id <> %s
                    """,
                    (
                        payload.service_id,
                        bill_type,
                        payload.room_type_id,
                        payload.doctor_id,
                        price_id,
                    ),
                )

                if cursor.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Another price already exists for this "
                            "service and pricing condition."
                        ),
                    )

                cursor.execute(
                    """
                    UPDATE service_prices
                    SET
                        service_id = %s,
                        bill_type = %s,
                        room_type_id = %s,
                        doctor_id = %s,
                        amount = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING
                        id,
                        service_id,
                        bill_type,
                        room_type_id,
                        doctor_id,
                        amount,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (
                        payload.service_id,
                        bill_type,
                        payload.room_type_id,
                        payload.doctor_id,
                        payload.amount,
                        price_id,
                    ),
                )

                return cursor.fetchone()

    except HTTPException:
        raise
    except errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="This pricing condition already exists.",
        )
    except psycopg2.Error:
        logger.exception("Failed to update service price.")
        raise HTTPException(
            status_code=500,
            detail="Unable to update the service price.",
        )
    finally:
        connection.close()


# ---------------------------------------------------------
# 6. ACTIVATE / DEACTIVATE SERVICE PRICE
# ---------------------------------------------------------

@router.patch("/service-prices/{price_id}/status")
def update_service_price_status(
    payload: ServicePriceStatusUpdate,
    price_id: int = Path(gt=0),
):
    """
    Change only the active status of a price.
    Does not change the service, bill type, room, doctor or amount.
    """

    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    UPDATE service_prices
                    SET
                        is_active = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING
                        id,
                        service_id,
                        bill_type,
                        room_type_id,
                        doctor_id,
                        amount,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (payload.is_active, price_id),
                )

                price = cursor.fetchone()

                if not price:
                    raise HTTPException(
                        status_code=404,
                        detail="Service price not found.",
                    )

                return price

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception(
            "Failed to update service price status for ID %s.",
            price_id,
        )
        raise HTTPException(
            status_code=500,
            detail="Unable to update the service price status.",
        )
    finally:
        connection.close()
