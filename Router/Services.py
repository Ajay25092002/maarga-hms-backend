
import logging
import os

import psycopg2
from psycopg2 import errors
from psycopg2.extras import RealDictCursor

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/services",
    tags=["Services"],
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

class ServiceCreate(BaseModel):
    service_group_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    available_for_op: bool = True
    available_for_ip: bool = False


class ServiceUpdate(BaseModel):
    service_group_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=255)
    available_for_op: bool
    available_for_ip: bool
    is_active: bool | None = None


class ServiceStatusUpdate(BaseModel):
    is_active: bool


# ---------------------------------------------------------
# VALIDATION HELPERS
# ---------------------------------------------------------

def validate_service_data(
    service_group_id: int,
    name: str,
    available_for_op: bool,
    available_for_ip: bool,
):
    name = name.strip()

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Service name cannot be empty.",
        )

    if not available_for_op and not available_for_ip:
        raise HTTPException(
            status_code=400,
            detail="A service must be available for OP, IP, or both.",
        )

    connection = get_db_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT id, name, is_active
                FROM service_groups
                WHERE id = %s
                """,
                (service_group_id,),
            )

            service_group = cursor.fetchone()

            if not service_group:
                raise HTTPException(
                    status_code=404,
                    detail="Service group not found.",
                )

            if not service_group["is_active"]:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot assign a service to an inactive service group.",
                )

        return name

    finally:
        connection.close()


# ---------------------------------------------------------
# 1. GET ALL SERVICES
# Supports search, filters, and server-side pagination.
# ---------------------------------------------------------

@router.get("")
def get_services(
    service_group_id: int | None = Query(default=None, gt=0),
    bill_type: str | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    search: str | None = Query(default=None, max_length=255),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
):
    if bill_type is not None:
        bill_type = bill_type.strip().upper()

        if bill_type not in ("OP", "IP"):
            raise HTTPException(
                status_code=400,
                detail="bill_type must be OP or IP.",
            )

    conditions = []
    filter_params = []

    if service_group_id is not None:
        conditions.append("s.service_group_id = %s")
        filter_params.append(service_group_id)

    if bill_type == "OP":
        conditions.append("s.available_for_op = TRUE")
    elif bill_type == "IP":
        conditions.append("s.available_for_ip = TRUE")

    if is_active is not None:
        conditions.append("s.is_active = %s")
        filter_params.append(is_active)

    if search and search.strip():
        conditions.append(
            "(s.name ILIKE %s OR sg.name ILIKE %s)"
        )
        search_pattern = f"%{search.strip()}%"
        filter_params.extend([search_pattern, search_pattern])

    where_clause = ""
    if conditions:
        where_clause = " WHERE " + " AND ".join(conditions)

    offset = (page - 1) * page_size
    connection = get_db_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            # Count all matching records before pagination.
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM services s
                INNER JOIN service_groups sg
                    ON sg.id = s.service_group_id
                {where_clause}
                """,
                filter_params,
            )

            total = cursor.fetchone()["total"]
            total_pages = (total + page_size - 1) // page_size

            # Retrieve only the requested page.
            cursor.execute(
                f"""
                SELECT
                    s.id,
                    s.service_group_id,
                    sg.name AS service_group_name,
                    s.name,
                    s.available_for_op,
                    s.available_for_ip,
                    s.is_active,
                    s.created_at,
                    s.updated_at
                FROM services s
                INNER JOIN service_groups sg
                    ON sg.id = s.service_group_id
                {where_clause}
                ORDER BY sg.name ASC, s.name ASC, s.id ASC
                LIMIT %s OFFSET %s
                """,
                filter_params + [page_size, offset],
            )

            items = cursor.fetchall()

            return {
                "items": items,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": total_pages,
                    "has_previous": page > 1,
                    "has_next": page < total_pages,
                },
            }

    except psycopg2.Error:
        logger.exception("Failed to retrieve services.")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve services.",
        )

    finally:
        connection.close()


# ---------------------------------------------------------
# 2. GET SERVICE BY ID
# ---------------------------------------------------------

@router.get("/{service_id}")
def get_service(
    service_id: int = Path(gt=0),
):
    connection = get_db_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    s.id,
                    s.service_group_id,
                    sg.name AS service_group_name,
                    s.name,
                    s.available_for_op,
                    s.available_for_ip,
                    s.is_active,
                    s.created_at,
                    s.updated_at
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

            return service

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Failed to retrieve service.")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve the service.",
        )

    finally:
        connection.close()


# ---------------------------------------------------------
# 3. CREATE SERVICE
# ---------------------------------------------------------

@router.post("", status_code=201)
def create_service(payload: ServiceCreate):
    name = validate_service_data(
        service_group_id=payload.service_group_id,
        name=payload.name,
        available_for_op=payload.available_for_op,
        available_for_ip=payload.available_for_ip,
    )

    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM services
                    WHERE service_group_id = %s
                      AND LOWER(name) = LOWER(%s)
                    """,
                    (payload.service_group_id, name),
                )

                if cursor.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A service with this name already exists "
                            "in the selected service group."
                        ),
                    )

                cursor.execute(
                    """
                    INSERT INTO services (
                        service_group_id,
                        name,
                        available_for_op,
                        available_for_ip,
                        is_active
                    )
                    VALUES (%s, %s, %s, %s, TRUE)
                    RETURNING
                        id,
                        service_group_id,
                        name,
                        available_for_op,
                        available_for_ip,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (
                        payload.service_group_id,
                        name,
                        payload.available_for_op,
                        payload.available_for_ip,
                    ),
                )

                return cursor.fetchone()

    except HTTPException:
        raise

    except errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="This service already exists in the selected group.",
        )

    except psycopg2.Error:
        logger.exception("Failed to create service.")
        raise HTTPException(
            status_code=500,
            detail="Unable to create the service.",
        )

    finally:
        connection.close()


# ---------------------------------------------------------
# 4. UPDATE SERVICE
# Supports updating is_active through PUT as well.
# ---------------------------------------------------------

@router.put("/{service_id}")
def update_service(
    payload: ServiceUpdate,
    service_id: int = Path(gt=0),
):
    name = validate_service_data(
        service_group_id=payload.service_group_id,
        name=payload.name,
        available_for_op=payload.available_for_op,
        available_for_ip=payload.available_for_ip,
    )

    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM services
                    WHERE id = %s
                    """,
                    (service_id,),
                )

                if not cursor.fetchone():
                    raise HTTPException(
                        status_code=404,
                        detail="Service not found.",
                    )

                cursor.execute(
                    """
                    SELECT id
                    FROM services
                    WHERE service_group_id = %s
                      AND LOWER(name) = LOWER(%s)
                      AND id <> %s
                    """,
                    (payload.service_group_id, name, service_id),
                )

                if cursor.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Another service with this name already "
                            "exists in the selected service group."
                        ),
                    )

                cursor.execute(
                    """
                    UPDATE services
                    SET
                        service_group_id = %s,
                        name = %s,
                        available_for_op = %s,
                        available_for_ip = %s,
                        is_active = COALESCE(%s, is_active),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING
                        id,
                        service_group_id,
                        name,
                        available_for_op,
                        available_for_ip,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (
                        payload.service_group_id,
                        name,
                        payload.available_for_op,
                        payload.available_for_ip,
                        payload.is_active,
                        service_id,
                    ),
                )

                return cursor.fetchone()

    except HTTPException:
        raise

    except errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="This service already exists in the selected group.",
        )

    except psycopg2.Error:
        logger.exception("Failed to update service.")
        raise HTTPException(
            status_code=500,
            detail="Unable to update the service.",
        )

    finally:
        connection.close()


# ---------------------------------------------------------
# 5. ACTIVATE / DEACTIVATE SERVICE
# PATCH /api/services/{service_id}/status
# ---------------------------------------------------------

@router.patch("/{service_id}/status")
def update_service_status(
    payload: ServiceStatusUpdate,
    service_id: int = Path(gt=0),
):
    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    UPDATE services
                    SET
                        is_active = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING
                        id,
                        service_group_id,
                        name,
                        available_for_op,
                        available_for_ip,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (payload.is_active, service_id),
                )

                service = cursor.fetchone()

                if not service:
                    raise HTTPException(
                        status_code=404,
                        detail="Service not found.",
                    )

                return service

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Failed to update service status.")
        raise HTTPException(
            status_code=500,
            detail="Unable to update the service status.",
        )

    finally:
        connection.close()
