
import logging
import os
import math

import psycopg2
from psycopg2 import errors
from psycopg2.extras import RealDictCursor

from fastapi import APIRouter, HTTPException, Path, Query
from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/room-types",
    tags=["Room Types"],
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
# RESPONSE HELPERS
# ---------------------------------------------------------

def room_type_response(room_type):
    """Convert database results into a JSON-friendly dictionary."""
    if room_type is None:
        return None

    return dict(room_type)


def pagination_response(
    items,
    total,
    page,
    page_size,
):
    """Build a consistent paginated response."""

    total_pages = math.ceil(total / page_size) if total else 0

    return {
        "items": [room_type_response(item) for item in items],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_previous": page > 1 and total_pages > 0,
            "has_next": page < total_pages,
        },
    }


# ---------------------------------------------------------
# REQUEST SCHEMAS
# ---------------------------------------------------------

class RoomTypeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)


class RoomTypeUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=150)


class RoomTypeStatusUpdate(BaseModel):
    is_active: bool


# ---------------------------------------------------------
# VALIDATION HELPERS
# ---------------------------------------------------------

def validate_room_type_name(name: str) -> str:
    name = name.strip()

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Room type name cannot be empty.",
        )

    if len(name) > 150:
        raise HTTPException(
            status_code=422,
            detail="Room type name cannot exceed 150 characters.",
        )

    return name


# ---------------------------------------------------------
# 1. GET ROOM TYPES
# SEARCH + STATUS FILTER + PAGINATION
# ---------------------------------------------------------

@router.get("")
def get_room_types(
    search: str | None = Query(default=None, max_length=150),
    is_active: bool | None = Query(default=None),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
):
    """
    Retrieve room types with optional search, status filtering,
    and server-side pagination.

    Examples:
        GET /api/room-types
        GET /api/room-types?search=private
        GET /api/room-types?is_active=true
        GET /api/room-types?page=1&page_size=10
        GET /api/room-types?search=private&page=1&page_size=10
        GET /api/room-types?is_active=false&page=1&page_size=10

    Compatibility:
        When neither page nor page_size is supplied, return a list.
        When either pagination parameter is supplied, return
        { "items": [...], "pagination": {...} }.
    """

    # If pagination is requested, use a default page size of 10.
    use_pagination = page is not None or page_size is not None

    current_page = page if page is not None else 1
    current_page_size = page_size if page_size is not None else 10

    if current_page_size > 100:
        raise HTTPException(
            status_code=422,
            detail="Page size cannot exceed 100.",
        )

    conditions = []
    params = []

    if is_active is not None:
        conditions.append("is_active = %s")
        params.append(is_active)

    if search and search.strip():
        conditions.append("name ILIKE %s")
        params.append(f"%{search.strip()}%")

    where_clause = ""

    if conditions:
        where_clause = " WHERE " + " AND ".join(conditions)

    connection = get_db_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:

            # Count matching records before pagination.
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM room_types
                {where_clause}
                """,
                params,
            )

            total = int(cursor.fetchone()["total"])

            query = f"""
                SELECT
                    id,
                    name,
                    is_active,
                    created_at,
                    updated_at
                FROM room_types
                {where_clause}
                ORDER BY name ASC, id ASC
            """

            query_params = list(params)

            if use_pagination:
                offset = (current_page - 1) * current_page_size

                query += """
                    LIMIT %s
                    OFFSET %s
                """

                query_params.extend(
                    [current_page_size, offset]
                )

            cursor.execute(query, query_params)
            room_types = cursor.fetchall()

            if use_pagination:
                return pagination_response(
                    items=room_types,
                    total=total,
                    page=current_page,
                    page_size=current_page_size,
                )

            return [room_type_response(item) for item in room_types]

    except psycopg2.Error:
        logger.exception("Failed to retrieve room types.")

        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve room types.",
        )

    finally:
        connection.close()


# ---------------------------------------------------------
# 2. GET ROOM TYPE BY ID
# ---------------------------------------------------------

@router.get("/{room_type_id}")
def get_room_type(
    room_type_id: int = Path(gt=0),
):
    """Retrieve a room type by its ID."""

    connection = get_db_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    name,
                    is_active,
                    created_at,
                    updated_at
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

            return room_type_response(room_type)

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Failed to retrieve room type.")

        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve the room type.",
        )

    finally:
        connection.close()


# ---------------------------------------------------------
# 3. CREATE ROOM TYPE
# ---------------------------------------------------------

@router.post("", status_code=201)
def create_room_type(payload: RoomTypeCreate):
    """Create a new room type."""

    name = validate_room_type_name(payload.name)
    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:

                # Prevent duplicate names, ignoring letter case.
                cursor.execute(
                    """
                    SELECT id
                    FROM room_types
                    WHERE LOWER(name) = LOWER(%s)
                    LIMIT 1
                    """,
                    (name,),
                )

                if cursor.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "A room type with this name already exists."
                        ),
                    )

                cursor.execute(
                    """
                    INSERT INTO room_types (
                        name,
                        is_active
                    )
                    VALUES (%s, TRUE)
                    RETURNING
                        id,
                        name,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (name,),
                )

                return room_type_response(cursor.fetchone())

    except HTTPException:
        raise

    except errors.UniqueViolation:
        logger.warning(
            "Duplicate room type creation attempted: %s",
            name,
        )

        raise HTTPException(
            status_code=409,
            detail="A room type with this name already exists.",
        )

    except psycopg2.Error:
        logger.exception("Failed to create room type.")

        raise HTTPException(
            status_code=500,
            detail="Unable to create the room type.",
        )

    finally:
        connection.close()


# ---------------------------------------------------------
# 4. UPDATE ROOM TYPE
# ---------------------------------------------------------

@router.put("/{room_type_id}")
def update_room_type(
    payload: RoomTypeUpdate,
    room_type_id: int = Path(gt=0),
):
    """Update an existing room type's name."""

    name = validate_room_type_name(payload.name)
    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:

                cursor.execute(
                    """
                    SELECT id
                    FROM room_types
                    WHERE id = %s
                    """,
                    (room_type_id,),
                )

                if not cursor.fetchone():
                    raise HTTPException(
                        status_code=404,
                        detail="Room type not found.",
                    )

                # Prevent duplicate names, excluding the current record.
                cursor.execute(
                    """
                    SELECT id
                    FROM room_types
                    WHERE LOWER(name) = LOWER(%s)
                      AND id <> %s
                    LIMIT 1
                    """,
                    (name, room_type_id),
                )

                if cursor.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Another room type already uses this name."
                        ),
                    )

                cursor.execute(
                    """
                    UPDATE room_types
                    SET
                        name = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING
                        id,
                        name,
                        is_active,
                        created_at,
                        updated_at
                    """,
                    (name, room_type_id),
                )

                return room_type_response(cursor.fetchone())

    except HTTPException:
        raise

    except errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="A room type with this name already exists.",
        )

    except psycopg2.Error:
        logger.exception("Failed to update room type.")

        raise HTTPException(
            status_code=500,
            detail="Unable to update the room type.",
        )

    finally:
        connection.close()


# ---------------------------------------------------------
# 5. ACTIVATE / DEACTIVATE ROOM TYPE
# ---------------------------------------------------------

@router.patch("/{room_type_id}/status")
def update_room_type_status(
    payload: RoomTypeStatusUpdate,
    room_type_id: int = Path(gt=0),
):
    """
    Activate or deactivate a room type without deleting it.

    Request body:
        { "is_active": true }
        { "is_active": false }
    """

    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(
                cursor_factory=RealDictCursor
            ) as cursor:

                cursor.execute(
                    """
                    UPDATE room_types
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
                    (payload.is_active, room_type_id),
                )

                room_type = cursor.fetchone()

                if not room_type:
                    raise HTTPException(
                        status_code=404,
                        detail="Room type not found.",
                    )

                return room_type_response(room_type)

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Failed to update room type status.")

        raise HTTPException(
            status_code=500,
            detail="Unable to update the room type status.",
        )

    finally:
        connection.close()
