
import base64
import hashlib
import hmac
import logging
import os
import secrets

import psycopg2
from dotenv import load_dotenv
from fastapi import HTTPException, status
from psycopg2 import IntegrityError
from psycopg2.extras import RealDictCursor

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# Database connection
# ---------------------------------------------------------

def get_db_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not configured in the .env file."
        )

    return psycopg2.connect(
        database_url,
        options="-c timezone=Asia/Kolkata",
        connect_timeout=10,
        cursor_factory=RealDictCursor,
    )


# ---------------------------------------------------------
# Password hashing
# ---------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)

    derived_key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=64,
    )

    salt_text = base64.b64encode(salt).decode("ascii")
    hash_text = base64.b64encode(derived_key).decode("ascii")

    return f"scrypt$16384$8$1${salt_text}${hash_text}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, hash_text = stored_hash.split("$")

        if algorithm != "scrypt":
            return False

        salt = base64.b64decode(salt_text, validate=True)
        expected_hash = base64.b64decode(hash_text, validate=True)

        actual_hash = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected_hash),
        )

        return hmac.compare_digest(actual_hash, expected_hash)

    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------
# Shared database helpers
# ---------------------------------------------------------

USER_SELECT = """
    SELECT
        u.id,
        u.full_name,
        u.email,
        u.role_id,
        r.name AS role_name,
        u.is_active,
        u.created_at,
        u.updated_at
    FROM public.users u
    JOIN public.roles r ON r.id = u.role_id
"""


def serialize_user(row):
    user = dict(row)

    for field in ("created_at", "updated_at"):
        if user.get(field) is not None:
            user[field] = user[field].isoformat()

    return user


def ensure_active_role(cursor, role_id: int):
    cursor.execute(
        """
        SELECT id, name
        FROM public.roles
        WHERE id = %s AND is_active = TRUE
        """,
        (role_id,),
    )

    role = cursor.fetchone()

    if role is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The selected role does not exist or is inactive.",
        )

    return role


def handle_database_error(error):
    constraint = getattr(
        getattr(error, "diag", None),
        "constraint_name",
        None,
    )

    if constraint in {
        "users_email_key",
        "idx_users_email_lower_unique",
    }:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This email address is already in use.",
        )

    if constraint == "users_role_id_fkey":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The selected role does not exist.",
        )

    logger.exception("Database constraint error while managing users.")

    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="Unable to complete the user operation.",
    )


# ---------------------------------------------------------
# List users
# ---------------------------------------------------------

def list_users_service(
    page: int = 1,
    page_size: int = 10,
    search: str | None = None,
    role_id: int | None = None,
    is_active: bool | None = None,
):
    connection = None

    try:
        connection = get_db_connection()

        where = []
        params = []

        if search and search.strip():
            term = f"%{search.strip()}%"

            where.append(
                """
                (
                    u.full_name ILIKE %s
                    OR COALESCE(u.email, '') ILIKE %s
                    OR r.name ILIKE %s
                )
                """
            )

            # Three SQL placeholders: full name, email, and role.
            params.extend([term] * 3)

        if role_id is not None:
            where.append("u.role_id = %s")
            params.append(role_id)

        if is_active is not None:
            where.append("u.is_active = %s")
            params.append(is_active)

        where_sql = (
            "WHERE " + " AND ".join(where)
            if where
            else ""
        )

        offset = (page - 1) * page_size

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT COUNT(*) AS total
                FROM public.users u
                JOIN public.roles r ON r.id = u.role_id
                {where_sql}
                """,
                tuple(params),
            )

            total = cursor.fetchone()["total"]

            cursor.execute(
                f"""
                {USER_SELECT}
                {where_sql}
                ORDER BY u.created_at DESC, u.id DESC
                LIMIT %s OFFSET %s
                """,
                tuple(params + [page_size, offset]),
            )

            users = [
                serialize_user(row)
                for row in cursor.fetchall()
            ]

        return {
            "items": users,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": (
                    (total + page_size - 1) // page_size
                    if page_size > 0
                    else 0
                ),
            },
        }

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Database error while listing users.")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve users.",
        )

    finally:
        if connection is not None:
            connection.close()


# ---------------------------------------------------------
# Get one user
# ---------------------------------------------------------

def get_user_service(user_id: int):
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                f"{USER_SELECT} WHERE u.id = %s",
                (user_id,),
            )

            user = cursor.fetchone()

            if user is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="User not found.",
                )

            return serialize_user(user)

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Database error retrieving user %s.", user_id)
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve the user.",
        )

    finally:
        if connection is not None:
            connection.close()


# ---------------------------------------------------------
# Create user
# ---------------------------------------------------------

def create_user_service(data: dict):
    connection = None

    try:
        connection = get_db_connection()

        with connection:
            with connection.cursor() as cursor:
                ensure_active_role(cursor, data["role_id"])

                password_hash = hash_password(data["password"])
                email = data.get("email")

                if email:
                    email = email.strip().lower()

                cursor.execute(
                    """
                    INSERT INTO public.users (
                        full_name,
                        email,
                        password_hash,
                        role_id,
                        is_active,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                    RETURNING id
                    """,
                    (
                        data["full_name"].strip(),
                        email,
                        password_hash,
                        data["role_id"],
                        data.get("is_active", True),
                    ),
                )

                user_id = cursor.fetchone()["id"]

        return get_user_service(user_id)

    except HTTPException:
        raise

    except IntegrityError as error:
        handle_database_error(error)

    except psycopg2.Error:
        logger.exception("Database error while creating a user.")
        raise HTTPException(
            status_code=500,
            detail="Unable to create the user.",
        )

    finally:
        if connection is not None:
            connection.close()


# ---------------------------------------------------------
# Update user
# ---------------------------------------------------------

def update_user_service(user_id: int, data: dict):
    connection = None

    allowed_fields = {
        "full_name",
        "email",
        "role_id",
        "is_active",
    }

    try:
        connection = get_db_connection()

        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM public.users WHERE id = %s",
                    (user_id,),
                )

                if cursor.fetchone() is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="User not found.",
                    )

                if "role_id" in data and data["role_id"] is not None:
                    ensure_active_role(cursor, data["role_id"])

                updates = {
                    key: value
                    for key, value in data.items()
                    if key in allowed_fields and value is not None
                }

                if updates.get("email"):
                    updates["email"] = updates["email"].strip().lower()

                if data.get("password"):
                    updates["password_hash"] = hash_password(
                        data["password"]
                    )

                if not updates:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail="No valid fields were provided for update.",
                    )

                assignments = []
                values = []

                for field, value in updates.items():
                    if field == "full_name":
                        value = value.strip()

                    assignments.append(f"{field} = %s")
                    values.append(value)

                assignments.append("updated_at = CURRENT_TIMESTAMP")
                values.append(user_id)

                cursor.execute(
                    f"""
                    UPDATE public.users
                    SET {", ".join(assignments)}
                    WHERE id = %s
                    """,
                    tuple(values),
                )

        return get_user_service(user_id)

    except HTTPException:
        raise

    except IntegrityError as error:
        handle_database_error(error)

    except psycopg2.Error:
        logger.exception("Database error updating user %s.", user_id)
        raise HTTPException(
            status_code=500,
            detail="Unable to update the user.",
        )

    finally:
        if connection is not None:
            connection.close()


# ---------------------------------------------------------
# Deactivate user
# ---------------------------------------------------------

def deactivate_user_service(user_id: int):
    connection = None

    try:
        connection = get_db_connection()

        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE public.users
                    SET is_active = FALSE,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING id
                    """,
                    (user_id,),
                )

                if cursor.fetchone() is None:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail="User not found.",
                    )

        return {
            "message": "User deactivated successfully.",
            "user_id": user_id,
        }

    except HTTPException:
        raise

    except psycopg2.Error:
        logger.exception("Database error deactivating user %s.", user_id)
        raise HTTPException(
            status_code=500,
            detail="Unable to deactivate the user.",
        )

    finally:
        if connection is not None:
            connection.close()


# ---------------------------------------------------------
# List active roles for user assignment
# ---------------------------------------------------------

def list_user_roles_service():
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, name
                FROM public.roles
                WHERE is_active = TRUE
                ORDER BY name ASC
                """
            )

            return [dict(row) for row in cursor.fetchall()]

    except psycopg2.Error:
        logger.exception("Database error while listing assignable roles.")
        raise HTTPException(
            status_code=500,
            detail="Unable to retrieve available roles.",
        )

    finally:
        if connection is not None:
            connection.close()
