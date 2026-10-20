
import os
import base64
import hashlib
import secrets
import logging

import psycopg2
from psycopg2.extras import RealDictCursor, Json
from dotenv import load_dotenv

load_dotenv(override=False)

logger = logging.getLogger(__name__)

ROLE_NAME = "Admin"

# Preserve the Admin permissions used by the application.
ADMIN_PERMISSIONS = {
    "dashboard": {
        "view": True,
    },
    "op_patients": {
        "view": True,
        "create": True,
        "edit": True,
        "delete": True,
    },
    "ip_patients": {
        "view": True,
        "create": True,
        "edit": True,
        "delete": True,
    },
    "op_bills": {
        "view": True,
        "create": True,
        "edit": True,
        "delete": True,
    },
    "ip_bills": {
        "view": True,
        "create": True,
        "edit": True,
        "delete": True,
    },
    "payments": {
        "view": True,
        "create": True,
        "edit": True,
        "delete": True,
    },
    "users": {
        "view": True,
        "create": True,
        "edit": True,
        "delete": True,
    },
    "roles": {
        "view": True,
        "create": True,
        "edit": True,
        "delete": True,
    },
    "pricing": {
        "view": True,
        "create": True,
        "edit": True,
        "delete": True,
    },
}


def hash_admin_password(password: str) -> str:
    """Hash the initial Admin password using scrypt."""
    salt = secrets.token_bytes(16)

    n = 16384
    r = 8
    p = 1

    derived_key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=64,
    )

    salt_b64 = base64.b64encode(salt).decode("utf-8")
    key_b64 = base64.b64encode(derived_key).decode("utf-8")

    return f"scrypt${n}${r}${p}${salt_b64}${key_b64}"


def create_initial_admin():
    """
    Create the initial Admin only when ADMIN_USER_ID does not already exist.

    If the configured User ID already exists, skip initial Admin creation
    and allow the application to continue starting.
    """

    database_url = os.getenv("DATABASE_URL")
    admin_user_id = os.getenv("ADMIN_USER_ID")
    admin_email = os.getenv("ADMIN_EMAIL")
    admin_full_name = os.getenv("ADMIN_FULL_NAME")
    admin_password = os.getenv("ADMIN_PASSWORD")

    required_values = {
        "DATABASE_URL": database_url,
        "ADMIN_USER_ID": admin_user_id,
        "ADMIN_EMAIL": admin_email,
        "ADMIN_FULL_NAME": admin_full_name,
        "ADMIN_PASSWORD": admin_password,
    }

    missing_values = [
        key for key, value in required_values.items()
        if not value or not value.strip()
    ]

    if missing_values:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing_values)
        )

    connection = None
    cursor = None

    try:
        connection = psycopg2.connect(database_url)
        cursor = connection.cursor(cursor_factory=RealDictCursor)

        # ---------------------------------------------------------
        # 1. If the configured User ID exists, skip creation.
        # ---------------------------------------------------------
        cursor.execute(
            """
            SELECT id, login_id
            FROM public.users
            WHERE login_id = %s
            LIMIT 1
            """,
            (admin_user_id,),
        )

        existing_user = cursor.fetchone()

        if existing_user:
            logger.info(
                "User ID '%s' already exists. "
                "Skipping initial Admin creation.",
                admin_user_id,
            )
            return

        # ---------------------------------------------------------
        # 2. If the email already exists, skip creation as well.
        # ---------------------------------------------------------
        cursor.execute(
            """
            SELECT id, login_id
            FROM public.users
            WHERE LOWER(email) = LOWER(%s)
            LIMIT 1
            """,
            (admin_email,),
        )

        existing_email = cursor.fetchone()

        if existing_email:
            logger.info(
                "Email '%s' already exists under User ID '%s'. "
                "Skipping initial Admin creation.",
                admin_email,
                existing_email["login_id"],
            )
            return

        # ---------------------------------------------------------
        # 3. Find or create the Admin role.
        # ---------------------------------------------------------
        cursor.execute(
            """
            SELECT id, name
            FROM public.roles
            WHERE LOWER(name) = LOWER(%s)
            LIMIT 1
            """,
            (ROLE_NAME,),
        )

        admin_role = cursor.fetchone()

        if admin_role:
            role_id = admin_role["id"]

            # Preserve/update the permissions required for Admin.
            cursor.execute(
                """
                UPDATE public.roles
                SET permissions = %s
                WHERE id = %s
                """,
                (Json(ADMIN_PERMISSIONS), role_id),
            )

        else:
            cursor.execute(
                """
                INSERT INTO public.roles (name, permissions)
                VALUES (%s, %s)
                RETURNING id
                """,
                (ROLE_NAME, Json(ADMIN_PERMISSIONS)),
            )

            admin_role = cursor.fetchone()
            role_id = admin_role["id"]

        # ---------------------------------------------------------
        # 4. Hash the password.
        # ---------------------------------------------------------
        password_hash = hash_admin_password(admin_password)

        # ---------------------------------------------------------
        # 5. Create the initial Admin user.
        # ---------------------------------------------------------
        cursor.execute(
            """
            INSERT INTO public.users (
                login_id,
                full_name,
                email,
                password_hash,
                role_id,
                is_active
            )
            VALUES (%s, %s, %s, %s, %s, TRUE)
            RETURNING id, login_id, full_name, email, role_id, is_active
            """,
            (
                admin_user_id,
                admin_full_name,
                admin_email,
                password_hash,
                role_id,
            ),
        )

        new_admin = cursor.fetchone()
        connection.commit()

        logger.info(
            "Initial Admin created successfully with User ID '%s'.",
            admin_user_id,
        )

        return new_admin

    except Exception:
        if connection:
            connection.rollback()
        logger.exception("Initial Admin initialization failed.")
        raise

    finally:
        if cursor:
            cursor.close()

        if connection:
            connection.close()
