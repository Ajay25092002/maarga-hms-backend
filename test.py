
import os
import base64
import hashlib
import secrets
import getpass
import json

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor, Json

load_dotenv()

EMAIL = "ajayjeyapal@gmail.com"
FULL_NAME = "Ajay"
ROLE_NAME = "Admin"

# Keep these permission keys consistent with your backend catalog.
PERMISSIONS = {
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


def hash_password(password: str) -> str:
    """Match the existing backend's scrypt password format."""
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


def main():
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is missing from your .env file."
        )

    password = getpass.getpass("Enter the user's password: ")

    if not 8 <= len(password) <= 128:
        raise ValueError("Password must contain 8–128 characters.")

    password_hash = hash_password(password)

    # The context manager commits on success and rolls back on error.
    with psycopg2.connect(
        database_url,
        connect_timeout=10,
        cursor_factory=RealDictCursor,
    ) as connection:
        with connection.cursor() as cursor:

            # Stop if the account already exists.
            cursor.execute(
                """
                SELECT id
                FROM public.users
                WHERE LOWER(email) = LOWER(%s)
                """,
                (EMAIL,),
            )

            existing_user = cursor.fetchone()

            if existing_user:
                raise RuntimeError(
                    f"User {EMAIL} already exists. "
                    "No changes were made."
                )

            # Create the Admin role if absent.
            cursor.execute(
                """
                SELECT id, permissions
                FROM public.roles
                WHERE LOWER(name) = LOWER(%s)
                """,
                (ROLE_NAME,),
            )

            role = cursor.fetchone()

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
                    RETURNING id, name
                    """,
                    (
                        ROLE_NAME,
                        "Full access to hospital billing and settings.",
                        Json(PERMISSIONS),
                    ),
                )
                role = cursor.fetchone()
                print(f"Created role: {role['name']}")

            else:
                # Do not silently elevate an existing role.
                if not role["permissions"]:
                    raise RuntimeError(
                        "An Admin role already exists without permissions. "
                        "Review it before assigning this user."
                    )

                cursor.execute(
                    """
                    SELECT is_active
                    FROM public.roles
                    WHERE id = %s
                    """,
                    (role["id"],),
                )

                if not cursor.fetchone()["is_active"]:
                    raise RuntimeError(
                        "The existing Admin role is inactive."
                    )

                print(
                    f"Using existing role ID {role['id']}. "
                    "Its permissions were not changed."
                )

            # Create the user using the selected role.
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
                RETURNING id, full_name, email, role_id, is_active
                """,
                (
                    FULL_NAME,
                    EMAIL,
                    password_hash,
                    role["id"],
                ),
            )

            user = cursor.fetchone()

    print("\nUser created successfully.")
    print(f"ID: {user['id']}")
    print(f"Name: {user['full_name']}")
    print(f"Email: {user['email']}")
    print(f"Role ID: {user['role_id']}")
    print(f"Active: {user['is_active']}")


if __name__ == "__main__":
    main()
