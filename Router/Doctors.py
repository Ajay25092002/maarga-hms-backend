# Router/Doctors.py

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
    prefix="/api/doctors",
    tags=["Doctors"],
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
# REQUEST SCHEMAS
# ---------------------------------------------------------

class DoctorCreate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class DoctorUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class DoctorStatusUpdate(BaseModel):
    is_active: bool


# ---------------------------------------------------------
# VALIDATION HELPERS
# ---------------------------------------------------------

def validate_doctor_name(name: str) -> str:
    name = name.strip()

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Doctor name cannot be empty.",
        )

    return name


# ---------------------------------------------------------
# 1. GET DOCTORS - SEARCH, FILTER AND PAGINATION
# ---------------------------------------------------------

@router.get("")
def get_doctors(
    is_active: bool | None = Query(default=None),
    search: str | None = Query(default=None, max_length=255),
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
):
    """
    Retrieve doctors with optional search, status filtering and pagination.

    Examples:
      GET /api/doctors
      GET /api/doctors?is_active=true
      GET /api/doctors?search=Arun
      GET /api/doctors?page=1&page_size=10
      GET /api/doctors?search=Arun&page=1&page_size=10

    Compatibility:
      - Without page/page_size, returns the existing plain list response.
      - When page or page_size is provided, returns {items, pagination}.
    """
    connection = get_db_connection()

    try:
        where_parts = ["1 = 1"]
        params = []

        if is_active is not None:
            where_parts.append("is_active = %s")
            params.append(is_active)

        if search and search.strip():
            where_parts.append("name ILIKE %s")
            params.append(f"%{search.strip()}%")

        where_sql = " AND ".join(where_parts)
        use_pagination = page is not None or page_size is not None

        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            if not use_pagination:
                cursor.execute(
                    f"""
                    SELECT id, name, is_active, created_at, updated_at
                    FROM doctors
                    WHERE {where_sql}
                    ORDER BY name ASC, id ASC
                    """,
                    params,
                )
                return cursor.fetchall()

            current_page = page or 1
            limit = page_size or 10
            offset = (current_page - 1) * limit

            cursor.execute(
                f"SELECT COUNT(*) AS total FROM doctors WHERE {where_sql}",
                params,
            )
            total = int(cursor.fetchone()["total"])
            total_pages = math.ceil(total / limit) if total else 0

            cursor.execute(
                f"""
                SELECT id, name, is_active, created_at, updated_at
                FROM doctors
                WHERE {where_sql}
                ORDER BY name ASC, id ASC
                LIMIT %s OFFSET %s
                """,
                params + [limit, offset],
            )
            items = cursor.fetchall()

            return {
                "items": items,
                "pagination": {
                    "page": current_page,
                    "page_size": limit,
                    "total": total,
                    "total_pages": total_pages,
                    "has_previous": current_page > 1,
                    "has_next": current_page < total_pages,
                },
            }

    except psycopg2.Error:
        logger.exception("Failed to retrieve doctors.")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve doctors.",
        )
    finally:
        connection.close()


# ---------------------------------------------------------
# 2. GET DOCTOR BY ID
# ---------------------------------------------------------

@router.get("/{doctor_id}")
def get_doctor(doctor_id: int = Path(gt=0)):
    connection = get_db_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT id, name, is_active, created_at, updated_at
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

            return doctor

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception("Failed to retrieve doctor.")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve the doctor.",
        )
    finally:
        connection.close()


# ---------------------------------------------------------
# 3. CREATE DOCTOR
# ---------------------------------------------------------

@router.post("", status_code=201)
def create_doctor(payload: DoctorCreate):
    name = validate_doctor_name(payload.name)
    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM doctors
                    WHERE LOWER(name) = LOWER(%s)
                    """,
                    (name,),
                )

                if cursor.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail="A doctor with this name already exists.",
                    )

                cursor.execute(
                    """
                    INSERT INTO doctors (name, is_active)
                    VALUES (%s, TRUE)
                    RETURNING id, name, is_active, created_at, updated_at
                    """,
                    (name,),
                )
                return cursor.fetchone()

    except HTTPException:
        raise
    except errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="A doctor with this name already exists.",
        )
    except psycopg2.Error:
        logger.exception("Failed to create doctor.")
        raise HTTPException(
            status_code=500,
            detail="Unable to create the doctor.",
        )
    finally:
        connection.close()


# ---------------------------------------------------------
# 4. UPDATE DOCTOR
# ---------------------------------------------------------

@router.put("/{doctor_id}")
def update_doctor(
    payload: DoctorUpdate,
    doctor_id: int = Path(gt=0),
):
    name = validate_doctor_name(payload.name)
    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT id FROM doctors WHERE id = %s",
                    (doctor_id,),
                )

                if not cursor.fetchone():
                    raise HTTPException(
                        status_code=404,
                        detail="Doctor not found.",
                    )

                cursor.execute(
                    """
                    SELECT id
                    FROM doctors
                    WHERE LOWER(name) = LOWER(%s)
                      AND id <> %s
                    """,
                    (name, doctor_id),
                )

                if cursor.fetchone():
                    raise HTTPException(
                        status_code=409,
                        detail="Another doctor already uses this name.",
                    )

                cursor.execute(
                    """
                    UPDATE doctors
                    SET name = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id, name, is_active, created_at, updated_at
                    """,
                    (name, doctor_id),
                )
                return cursor.fetchone()

    except HTTPException:
        raise
    except errors.UniqueViolation:
        raise HTTPException(
            status_code=409,
            detail="A doctor with this name already exists.",
        )
    except psycopg2.Error:
        logger.exception("Failed to update doctor.")
        raise HTTPException(
            status_code=500,
            detail="Unable to update the doctor.",
        )
    finally:
        connection.close()


# ---------------------------------------------------------
# 5. ACTIVATE / DEACTIVATE DOCTOR
# ---------------------------------------------------------

@router.patch("/{doctor_id}/status")
def update_doctor_status(
    payload: DoctorStatusUpdate,
    doctor_id: int = Path(gt=0),
):
    """
    Activate/deactivate a doctor without deleting the record.

    Frontend request:
      PATCH /api/doctors/{doctor_id}/status
      JSON body: {"is_active": false}
    """
    connection = get_db_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    UPDATE doctors
                    SET is_active = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id, name, is_active, created_at, updated_at
                    """,
                    (payload.is_active, doctor_id),
                )
                doctor = cursor.fetchone()

                if not doctor:
                    raise HTTPException(
                        status_code=404,
                        detail="Doctor not found.",
                    )

                return doctor

    except HTTPException:
        raise
    except psycopg2.Error:
        logger.exception("Failed to update doctor status.")
        raise HTTPException(
            status_code=500,
            detail="Unable to update the doctor status.",
        )
    finally:
        connection.close()
