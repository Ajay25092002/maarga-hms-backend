
import os

import psycopg2
from psycopg2 import errors
from psycopg2.extras import Json, RealDictCursor

from Schemas.role_schema import ROLE_PERMISSIONS


class RoleNotFoundError(Exception):
    pass


class DuplicateRoleError(Exception):
    pass


class RoleInUseError(Exception):
    pass


def _get_connection():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError("DATABASE_URL is not configured.")

    return psycopg2.connect(database_url, options="-c timezone=Asia/Kolkata")


def _normalize_role(row):
    role = dict(row)
    stored = role.get("permissions") or {}

    # Older roles may not have every permission key yet.
    role["permissions"] = {
        key: stored.get(key, False)
        for key in ROLE_PERMISSIONS
    }

    return role


def list_roles():
    connection = _get_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("""
                SELECT
                    id, name, description, permissions,
                    is_active, created_at, updated_at
                FROM public.roles
                ORDER BY created_at DESC, id DESC;
            """)
            return [_normalize_role(row) for row in cursor.fetchall()]
    finally:
        connection.close()


def get_role(role_id: int):
    connection = _get_connection()

    try:
        with connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("""
                SELECT
                    id, name, description, permissions,
                    is_active, created_at, updated_at
                FROM public.roles
                WHERE id = %s;
            """, (role_id,))

            row = cursor.fetchone()

            if row is None:
                raise RoleNotFoundError("Role not found.")

            return _normalize_role(row)
    finally:
        connection.close()


def create_role(data: dict):
    connection = _get_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    INSERT INTO public.roles (
                        name, description, permissions, is_active
                    )
                    VALUES (%s, %s, %s, %s)
                    RETURNING
                        id, name, description, permissions,
                        is_active, created_at, updated_at;
                """, (
                    data["name"],
                    data.get("description"),
                    Json(data["permissions"]),
                    data.get("is_active", True),
                ))

                return _normalize_role(cursor.fetchone())

    except errors.UniqueViolation as exc:
        raise DuplicateRoleError(
            "A role with this name already exists."
        ) from exc
    finally:
        connection.close()


def update_role(role_id: int, data: dict):
    connection = _get_connection()

    try:
        with connection:
            with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute("""
                    UPDATE public.roles
                    SET
                        name = %s,
                        description = %s,
                        permissions = %s,
                        is_active = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    RETURNING
                        id, name, description, permissions,
                        is_active, created_at, updated_at;
                """, (
                    data["name"],
                    data.get("description"),
                    Json(data["permissions"]),
                    data.get("is_active", True),
                    role_id,
                ))

                row = cursor.fetchone()

                if row is None:
                    raise RoleNotFoundError("Role not found.")

                return _normalize_role(row)

    except errors.UniqueViolation as exc:
        raise DuplicateRoleError(
            "A role with this name already exists."
        ) from exc
    finally:
        connection.close()


def delete_role(role_id: int):
    connection = _get_connection()

    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute("""
                    DELETE FROM public.roles
                    WHERE id = %s;
                """, (role_id,))

                if cursor.rowcount == 0:
                    raise RoleNotFoundError("Role not found.")

    except errors.ForeignKeyViolation as exc:
        raise RoleInUseError(
            "This role is assigned to users and cannot be deleted."
        ) from exc
    finally:
        connection.close()

    return {"message": "Role deleted successfully."}
