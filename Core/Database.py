import logging
import os

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from dotenv import load_dotenv

load_dotenv(override=False)

logger = logging.getLogger(__name__)


def initialize_database():
    """Initialize the database and apply safe additive migrations."""

    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not configured in the .env file."
        )

    config = parse_dsn(database_url)
    database_name = config.get("dbname")

    if not database_name:
        raise RuntimeError(
            "DATABASE_URL must include a database name."
        )

    # Create the target database if it does not exist.
    maintenance_config = config.copy()
    maintenance_config["dbname"] = "postgres"
    connection = None

    try:
        connection = psycopg2.connect(**maintenance_config)
        connection.autocommit = True

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (database_name,),
            )

            if cursor.fetchone() is None:
                cursor.execute(
                    sql.SQL("CREATE DATABASE {}").format(
                        sql.Identifier(database_name)
                    )
                )
                logger.info("Created database: %s", database_name)
            else:
                logger.info("Database already exists: %s", database_name)

    finally:
        if connection is not None:
            connection.close()

    connection = None

    try:
        connection = psycopg2.connect(**config)

        with connection:
            with connection.cursor() as cursor:

                # --------------------------------------------------
                # Patients
                # --------------------------------------------------
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.patients (
                        id BIGSERIAL PRIMARY KEY,
                        patient_id VARCHAR(50) NOT NULL UNIQUE,
                        registration_number VARCHAR(100) UNIQUE,
                        name VARCHAR(201) NOT NULL,
                        date_of_birth DATE,
                        gender VARCHAR(30),
                        mobile VARCHAR(20) NOT NULL,
                        email VARCHAR(255),
                        address TEXT,
                        registered_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                cursor.execute("""
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'patients';
                """)

                patient_columns = {
                    row[0] for row in cursor.fetchall()
                }

                required_patient_columns = {
                    "id",
                    "patient_id",
                    "registration_number",
                    "name",
                    "date_of_birth",
                    "gender",
                    "mobile",
                    "email",
                    "address",
                    "registered_at",
                }

                missing_columns = (
                    required_patient_columns - patient_columns
                )

                if missing_columns:
                    raise RuntimeError(
                        "The patients table is missing required columns: "
                        + ", ".join(sorted(missing_columns))
                    )

                if {"first_name", "last_name"} & patient_columns:
                    logger.warning(
                        "Legacy first_name/last_name columns still exist. "
                        "Complete the reviewed migration before using "
                        "the new patient schema."
                    )

                # --------------------------------------------------
                # Bills
                # --------------------------------------------------
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.bills (
                        id BIGSERIAL PRIMARY KEY,
                        bill_number VARCHAR(100) NOT NULL UNIQUE,
                        bill_type VARCHAR(2) NOT NULL
                            CHECK (bill_type IN ('OP', 'IP')),
                        patient_db_id BIGINT NOT NULL
                            REFERENCES public.patients(id),
                        registration_number VARCHAR(100),
                        bill_date TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        department VARCHAR(100),
                        doctor_name VARCHAR(150),
                        subtotal NUMERIC(12, 2) NOT NULL DEFAULT 0
                            CHECK (subtotal >= 0),
                        discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0
                            CHECK (discount_amount >= 0),
                        total_amount NUMERIC(12, 2) NOT NULL DEFAULT 0
                            CHECK (total_amount >= 0),
                        notes TEXT,
                        admission_date TIMESTAMPTZ,
                        discharge_date TIMESTAMPTZ,
                        bed_number VARCHAR(100),
                        rate_category VARCHAR(100),
                        patient_category VARCHAR(100),
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # --------------------------------------------------
                # Bill items
                # --------------------------------------------------
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.bill_items (
                        id BIGSERIAL PRIMARY KEY,
                        bill_id BIGINT NOT NULL
                            REFERENCES public.bills(id) ON DELETE CASCADE,
                        category VARCHAR(100),
                        service_date DATE,
                        service_code VARCHAR(100),
                        description TEXT NOT NULL,
                        rate NUMERIC(12, 2) NOT NULL DEFAULT 0
                            CHECK (rate >= 0),
                        quantity NUMERIC(10, 2) NOT NULL DEFAULT 1
                            CHECK (quantity > 0),
                        amount NUMERIC(12, 2) NOT NULL DEFAULT 0
                            CHECK (amount >= 0),
                        discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0
                            CHECK (discount_amount >= 0),
                        net_amount NUMERIC(12, 2) NOT NULL DEFAULT 0
                            CHECK (net_amount >= 0),
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                cursor.execute("""
                    ALTER TABLE public.bill_items
                    ADD COLUMN IF NOT EXISTS category VARCHAR(100);
                """)

                # --------------------------------------------------
                # Payments
                # --------------------------------------------------
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.bill_payments (
                        id BIGSERIAL PRIMARY KEY,
                        bill_id BIGINT NOT NULL
                            REFERENCES public.bills(id) ON DELETE RESTRICT,
                        amount NUMERIC(12, 2) NOT NULL
                            CHECK (amount > 0),
                        payment_date TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        payment_method VARCHAR(50) NOT NULL,
                        reference_number VARCHAR(150),
                        notes TEXT,
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                # Additive migrations for existing payment tables.
                cursor.execute("""
                    ALTER TABLE public.bill_payments
                    ADD COLUMN IF NOT EXISTS reference_number VARCHAR(150);
                """)

                cursor.execute("""
                    ALTER TABLE public.bill_payments
                    ADD COLUMN IF NOT EXISTS notes TEXT;
                """)

                cursor.execute("""
                    ALTER TABLE public.bill_payments
                    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
                    NOT NULL DEFAULT CURRENT_TIMESTAMP;
                """)

                # --------------------------------------------------
                # Roles
                # --------------------------------------------------
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.roles (
                        id BIGSERIAL PRIMARY KEY,
                        name VARCHAR(100) NOT NULL UNIQUE,
                        description TEXT,
                        permissions JSONB NOT NULL DEFAULT '{}'::JSONB,
                        is_active BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_roles_is_active
                    ON public.roles (is_active);
                """)

                # --------------------------------------------------
                # Users
                # Each user is assigned one role.
                # Permissions are maintained in roles.permissions.
                # --------------------------------------------------
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.users (
                        id BIGSERIAL PRIMARY KEY,

                        full_name VARCHAR(150) NOT NULL,


                        email VARCHAR(255) UNIQUE,

                        password_hash TEXT NOT NULL,

                        role_id BIGINT NOT NULL
                            REFERENCES public.roles(id)
                            ON DELETE RESTRICT,

                        is_active BOOLEAN NOT NULL DEFAULT TRUE,

                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,

                        updated_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,

                        CONSTRAINT users_full_name_not_blank
                            CHECK (length(trim(full_name)) > 0)

                
                    );
                """)

                # User lookup indexes.
                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_users_role_id
                    ON public.users (role_id);
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_users_is_active
                    ON public.users (is_active);
                """)

                # --------------------------------------------------
                # Receipt sequence
                # Independent of bill_payments.id.
                # --------------------------------------------------
                cursor.execute("""
                    CREATE SEQUENCE IF NOT EXISTS
                        public.bill_payment_receipt_seq
                    AS BIGINT
                    START WITH 1
                    INCREMENT BY 1;
                """)

                cursor.execute("""
                    ALTER TABLE public.bill_payments
                    ADD COLUMN IF NOT EXISTS receipt_number VARCHAR(30);
                """)

                # Only synchronize and backfill when receipt numbers
                # are missing. Avoid resetting the sequence on startup.
                cursor.execute("""
                    SELECT COUNT(*)
                    FROM public.bill_payments
                    WHERE receipt_number IS NULL;
                """)

                missing_receipts = cursor.fetchone()[0]

                if missing_receipts:
                    cursor.execute("""
                        SELECT COALESCE(
                            MAX(
                                CASE
                                    WHEN receipt_number ~ '^RC-[0-9]+$'
                                    THEN SUBSTRING(
                                        receipt_number FROM 4
                                    )::BIGINT
                                END
                            ),
                            0
                        )
                        FROM public.bill_payments;
                    """)

                    max_receipt_number = cursor.fetchone()[0]

                    if max_receipt_number > 0:
                        cursor.execute(
                            """
                            SELECT setval(
                                'public.bill_payment_receipt_seq',
                                %s,
                                TRUE
                            );
                            """,
                            (max_receipt_number,),
                        )
                    else:
                        cursor.execute("""
                            SELECT setval(
                                'public.bill_payment_receipt_seq',
                                1,
                                FALSE
                            );
                        """)

                    cursor.execute("""
                        UPDATE public.bill_payments
                        SET receipt_number =
                            'RC-' || LPAD(
                                nextval(
                                    'public.bill_payment_receipt_seq'
                                )::TEXT,
                                4,
                                '0'
                            )
                        WHERE receipt_number IS NULL;
                    """)

                cursor.execute("""
                    ALTER TABLE public.bill_payments
                    ALTER COLUMN receipt_number
                    SET DEFAULT (
                        'RC-' || LPAD(
                            nextval(
                                'public.bill_payment_receipt_seq'
                            )::TEXT,
                            4,
                            '0'
                        )
                    );
                """)

                cursor.execute("""
                    ALTER TABLE public.bill_payments
                    ALTER COLUMN receipt_number SET NOT NULL;
                """)

                cursor.execute("""
                    CREATE UNIQUE INDEX IF NOT EXISTS
                        uq_bill_payments_receipt_number
                    ON public.bill_payments (receipt_number);
                """)

                # --------------------------------------------------
                # Search and relationship indexes
                # --------------------------------------------------
                index_statements = [
                    """
                    CREATE INDEX IF NOT EXISTS idx_patients_name
                    ON public.patients (name)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_patients_mobile
                    ON public.patients (mobile)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_patients_registered_at
                    ON public.patients (registered_at)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_bills_patient_db_id
                    ON public.bills (patient_db_id)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_bills_bill_date
                    ON public.bills (bill_date)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_bills_bill_type
                    ON public.bills (bill_type)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_bills_registration_number
                    ON public.bills (registration_number)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_bill_items_bill_id
                    ON public.bill_items (bill_id)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_bill_payments_bill_id
                    ON public.bill_payments (bill_id)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_bill_payments_payment_date
                    ON public.bill_payments (payment_date)
                    """,
                    """
                    CREATE INDEX IF NOT EXISTS idx_bill_payments_method
                    ON public.bill_payments (payment_method)
                    """,
                ]

                for statement in index_statements:
                    cursor.execute(statement)

        logger.info("Database initialization completed successfully.")

    except Exception:
        logger.exception("Database initialization failed.")
        raise

    finally:
        if connection is not None:
            connection.close()