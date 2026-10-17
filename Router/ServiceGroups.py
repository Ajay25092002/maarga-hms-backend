
import logging
import os

import psycopg2
from psycopg2 import errors
from psycopg2.extras import RealDictCursor

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/service-groups",
    tags=["Service Groups"],
)


# --------------------------------------------------
# Database Connection
# --------------------------------------------------

def get_db_connection():
    """Connect to PostgreSQL using DATABASE_URL."""
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise HTTPException(
            status_code=500,
            detail="DATABASE_URL is not configured.",
        )

    try:
        return psycopg2.connect(database_url, options="-c timezone=Asia/Kolkata")
    except psycopg2.Error:
        logger.exception("Failed to connect to the database.")
        raise HTTPException(
            status_code=500,
            detail="Unable to connect to the database.",
        )


# --------------------------------------------------
# Request Schemas
# --------------------------------------------------

class ServiceGroupCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)


class ServiceGroupUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    is_active: bool | None = None


class ServiceGroupStatusUpdate(BaseModel):
    is_active: bool


# --------------------------------------------------
# GET: List Service Groups
# GET /api/service-groups
#
# Supports:
#   ?page=1&page_size=10
#   ?search=lab
#   ?is_active=true
#   ?search=lab&is_active=true&page=1&page_size=10
# --------------------------------------------------

@router.get("")
def get_service_groups(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
    search: str | None = Query(default=None, max_length=150),
    is_active: bool | None = Query(default=None),
):
    connection = get_db_connection()

    try:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:

            # Build the same filters for the count and data queries.
            filters = []
            params = []

            if is_active is not None:
                filters.append("is_active = %s")
                params.append(is_active)

            if search and search.strip():
                filters.append("name ILIKE %s")
                params.append(f"%{search.strip()}%")

            where_clause = ""

            if filters:
                where_clause = " WHERE " + " AND ".join(filters)

            # Count matching records before applying pagination.
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM public.service_groups
                {where_clause}
                """,
                params,
            )

            total = cursor.fetchone()["total"]

            # Calculate pagination information.
            total_pages = (total + page_size - 1) // page_size
            offset = (page - 1) * page_size

            # Retrieve only the requested page.
            cursor.execute(
                f"""
                SELECT
                    id,
                    name,
                    is_active,
                    created_at,
                    updated_at
                FROM public.service_groups
                {where_clause}
                ORDER BY name ASC, id ASC
                LIMIT %s OFFSET %s
                """,
                params + [page_size, offset],
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
        logger.exception("Failed to retrieve service groups.")
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve service groups.",
        )
    finally:
        connection.close()


# --------------------------------------------------
# GET: Get One Service Group
# GET /api/service-groups/{service_group_id}
# --------------------------------------------------

@router.get("/{service_group_id}")
def get_service_group(
    service_group_id: int = Path(ge=1),
):
    connection = get_db_connection()

    try:
        with connection.cursor(
            cursor_factory=RealDictCursor
        ) as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    name,
                    is_active,
                    created_at,
                    updated_at
                FROM public.service_groups
                WHERE id = %s
                """,
                (service_group_id,),
            )

            service_group = cursor.fetchone()

            if service_group is None:
                raise HTTPException(
                    status_code=404,
                    detail="Service group not found.",
                )

            return service_group

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception("Failed to retrieve service group.")
        raise HTTPException(
            status_code=500,
            detail="Failed to retrieve service group.",
        )
    finally:
        connection.close()


# --------------------------------------------------
# POST: Create Service Group
# POST /api/service-groups
# --------------------------------------------------

@router.post("", status_code=201)
def create_service_group(
    payload: ServiceGroupCreate,
):
    name = payload.name.strip()

    if not name:
        raise HTTPException(
            status_code=422,
            detail="Service group name cannot be empty.",
        )

    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM public.service_groups
                    WHERE LOWER(name) = LOWER(%s)
                    """,
                    (name,),
                )

                if cursor.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A service group with this name "
                            "already exists."
                        ),
                    )

                cursor.execute(
                    """
                    INSERT INTO public.service_groups (name)
                    VALUES (%s)
                    RETURNING
                        id,
                        name,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (name,),
                )

                return cursor.fetchone()

    except HTTPException:
        raise
    except errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="A service group with this name already exists.",
        )
    except psycopg2.Error:
        logger.exception("Failed to create service group.")
        raise HTTPException(
            status_code=500,
            detail="Failed to create service group.",
        )
    finally:
        connection.close()


# --------------------------------------------------
# PUT: Update Service Group
# PUT /api/service-groups/{service_group_id}
#
# Updates the name and optionally the active status.
# --------------------------------------------------

@router.put("/{service_group_id}")
def update_service_group(
    payload: ServiceGroupUpdate,
    service_group_id: int = Path(ge=1),
):
    name = payload.name.strip()

    if not name:
        raise HTTPException(
            status_code=422,
            detail="Service group name cannot be empty.",
        )

    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:

                cursor.execute(
                    """
                    SELECT id
                    FROM public.service_groups
                    WHERE LOWER(name) = LOWER(%s)
                      AND id != %s
                    """,
                    (name, service_group_id),
                )

                if cursor.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Another service group already "
                            "uses this name."
                        ),
                    )

                cursor.execute(
                    """
                    UPDATE public.service_groups
                    SET
                        name = %s,
                        is_active = COALESCE(%s, is_active),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING
                        id,
                        name,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (
                        name,
                        payload.is_active,
                        service_group_id,
                    ),
                )

                service_group = cursor.fetchone()

                if service_group is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Service group not found.",
                    )

                return service_group

    except HTTPException:
        raise
    except errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="Another service group already uses this name.",
        )
    except psycopg2.Error:
        logger.exception("Failed to update service group.")
        raise HTTPException(
            status_code=500,
            detail="Failed to update service group.",
        )
    finally:
        connection.close()


# --------------------------------------------------
# PATCH: Activate / Deactivate Service Group
# PATCH /api/service-groups/{service_group_id}/status
# --------------------------------------------------

@router.patch("/{service_group_id}/status")
def update_service_group_status(
    payload: ServiceGroupStatusUpdate,
    service_group_id: int = Path(ge=1),
):
    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:
                cursor.execute(
                    """
                    UPDATE public.service_groups
                    SET
                        is_active = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING
                        id,
                        name,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (payload.is_active, service_group_id),
                )

                service_group = cursor.fetchone()

                if service_group is None:
                    raise HTTPException(
                        status_code=404,
                        detail="Service group not found.",
                    )

                return service_group

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception(
            "Failed to update service group status."
        )
        raise HTTPException(
            status_code=500,
            detail="Failed to update service group status.",
        )
    finally:
        connection.close()
