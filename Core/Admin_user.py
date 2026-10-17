
import os
import base64
import hashlib
import secrets
import logging

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor, Json

load_dotenv()

logger = logging.getLogger(__name__)


# ---------------------------------------------------------
# ADMIN CONFIGURATION
# ---------------------------------------------------------

ROLE_NAME = "Admin"

ADMIN_PERMISSIONS = {
    "dashboard.view": True,

    "patients.op.create": True,
    "patients.op.view": True,
    "patients.ip.create": True,
    "patients.ip.view": True,

    "bills.op.view": True,
    "bills.ip.view": True,
    "payments.view": True,

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


# ---------------------------------------------------------
# PASSWORD HASHING
# ---------------------------------------------------------

def hash_admin_password(password: str) -> str:
    """
    Hash the password using the application's scrypt format.

    Make sure this format matches the existing authentication
    code used to verify user passwords.
    """
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

    return (
        f"scrypt$16384$8$1${salt_text}${hash_text}"
    )


# ---------------------------------------------------------
# CREATE INITIAL ADMIN USER
# ---------------------------------------------------------

def create_initial_admin() -> None:
    """
    Create the initial Admin role and user.

    Behavior:
    - Runs after database initialization.
    - Creates the Admin role if it does not exist.
    - Creates the Admin user if the email does not exist.
    - Does not overwrite an existing user's password.
    - Does not silently modify existing role permissions.
    """

    database_url = os.getenv("DATABASE_URL")

    admin_email = os.getenv("ADMIN_EMAIL", "").strip()
    admin_full_name = os.getenv("ADMIN_FULL_NAME", "").strip()
    admin_password = os.getenv("ADMIN_PASSWORD", "")

    # Validate configuration before connecting to the database.
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is missing from the environment."
        )

    if not admin_email:
        raise RuntimeError(
            "ADMIN_EMAIL is missing from the environment."
        )

    if not admin_full_name:
        raise RuntimeError(
            "ADMIN_FULL_NAME is missing from the environment."
        )

    if not 8 <= len(admin_password) <= 128:
        raise RuntimeError(
            "ADMIN_PASSWORD must contain between 8 and 128 characters."
        )

    password_hash = hash_admin_password(admin_password)

    # Connect to PostgreSQL.
    with psycopg2.connect(
        database_url,
        options="-c timezone=Asia/Kolkata",
        connect_timeout=10,
        cursor_factory=RealDictCursor,
    ) as connection:

        with connection.cursor() as cursor:

            # -------------------------------------------------
            # 1. CHECK WHETHER THE ADMIN USER ALREADY EXISTS
            # -------------------------------------------------

            cursor.execute(
                """
                SELECT id
                FROM public.users
                WHERE LOWER(email) = LOWER(%s)
                """,
                (admin_email,),
            )

            existing_user = cursor.fetchone()

            if existing_user:
                logger.info(
                    "Admin user already exists. "
                    "Skipping user creation."
                )
                return

            # -------------------------------------------------
            # 2. FIND THE ADMIN ROLE
            # -------------------------------------------------

            cursor.execute(
                """
                SELECT
                    id,
                    name,
                    permissions,
                    is_active
                FROM public.roles
                WHERE LOWER(name) = LOWER(%s)
                FOR UPDATE
                """,
                (ROLE_NAME,),
            )

            role = cursor.fetchone()

            # -------------------------------------------------
            # 3. CREATE THE ADMIN ROLE IF IT DOES NOT EXIST
            # -------------------------------------------------

            if role is None:

                cursor.execute(
                    """
                    INSERT INTO public.roles (
                        name,
                        description,
                        permissions,
                        is_active
                    )
                    VALUES (%s, %s, %s, TRUE)
                    RETURNING
                        id,
                        name,
                        permissions,
                        is_active
                    """,
                    (
                        ROLE_NAME,
                        "Full access to hospital billing and settings.",
                        Json(ADMIN_PERMISSIONS),
                    ),
                )

                role = cursor.fetchone()

                logger.info(
                    "Admin role created successfully. Role ID: %s",
                    role["id"],
                )

            else:

                # Do not silently activate an inactive role.
                if not role["is_active"]:
                    raise RuntimeError(
                        "The existing Admin role is inactive. "
                        "Activate it explicitly before creating "
                        "the Admin user."
                    )

                # Do not silently elevate or repair existing permissions.
                if not role["permissions"]:
                    raise RuntimeError(
                        "The existing Admin role has no permissions. "
                        "Review its permissions before proceeding."
                    )

                logger.info(
                    "Using existing Admin role ID %s. "
                    "Existing permissions will not be modified.",
                    role["id"],
                )

            # -------------------------------------------------
            # 4. CREATE THE ADMIN USER
            # -------------------------------------------------

            cursor.execute(
                """
                INSERT INTO public.users (
                    full_name,
                    email,
                    password_hash,
                    role_id,
                    is_active
                )
                VALUES (%s, %s, %s, %s, TRUE)
                RETURNING
                    id,
                    full_name,
                    email,
                    role_id,
                    is_active
                """,
                (
                    admin_full_name,
                    admin_email,
                    password_hash,
                    role["id"],
                ),
            )

            user = cursor.fetchone()

    # The database transaction has committed successfully here.
    logger.info(
        "Initial Admin user created successfully. User ID: %s",
        user["id"],
    )
    logger.info(
        "Admin email: %s",
        user["email"],
    )


# ---------------------------------------------------------
# OPTIONAL MANUAL EXECUTION
# ---------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    try:
        create_initial_admin()
        print("Admin initialization completed successfully.")

    except Exception:
        logger.exception("Admin initialization failed.")
        raise
