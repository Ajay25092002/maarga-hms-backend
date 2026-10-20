
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

# Keep permission keys consistent with ROLE_PERMISSIONS.
ADMIN_PERMISSIONS = {
    "dashboard.view": True,

    "patients.op.view": True,
    "patients.op.create": True,
    "patients.op.edit": True,

    "patients.ip.view": True,
    "patients.ip.create": True,
    "patients.ip.edit": True,

    "bills.op.view": True,
    "bills.op.create": True,
    "bills.op.edit": True,

    "bills.ip.view": True,
    "bills.ip.create": True,
    "bills.ip.edit": True,

    "nurse.view": True,
    "nurse.add_services": True,

    "payments.view": True,
    "payments.create": True,

    "refunds.view": True,
    "refunds.create": True,

    "reports.view": True,
    "reports.download": True,

    "users.view": True,
    "users.create": True,
    "users.edit": True,
    "users.delete": True,

    "roles.view": True,
    "roles.create": True,
    "roles.edit": True,
    "roles.delete": True,

    "pricing.view": True,
    "pricing.create": True,
    "pricing.edit": True,
    "pricing.import": True,
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
    Create the initial Admin when the configured login ID and email
    do not already exist.

    Existing users are not modified. The Admin role permissions are
    synchronized when a new initial Admin user is created.
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
        key
        for key, value in required_values.items()
        if not value or not value.strip()
    ]

    if missing_values:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing_values)
        )

    admin_user_id = admin_user_id.strip()
    admin_email = admin_email.strip()
    admin_full_name = admin_full_name.strip()

    connection = None
    cursor = None

    try:
        connection = psycopg2.connect(database_url)
        cursor = connection.cursor(cursor_factory=RealDictCursor)

        # 1. Check whether the configured login ID already exists.
        cursor.execute(
            """
            SELECT id, login_id
            FROM public.users
            WHERE LOWER(login_id) = LOWER(%s)
            LIMIT 1
            """,
            (admin_user_id,),
        )

        existing_user = cursor.fetchone()

        if existing_user:
            logger.info(
                "User ID '%s' already exists. "
                "Skipping initial Admin creation.",
                existing_user["login_id"],
            )
            connection.rollback()
            return

        # 2. Prevent duplicate email addresses.
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
            connection.rollback()
            return

        # 3. Find or create the Admin role.
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

            # Synchronize permissions with the canonical catalog.
            cursor.execute(
                """
                UPDATE public.roles
                SET permissions = %s,
                    updated_at = CURRENT_TIMESTAMP
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

        # 4. Hash the configured Admin password.
        password_hash = hash_admin_password(admin_password)

        # 5. Create the initial Admin user.
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
