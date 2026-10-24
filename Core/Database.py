import logging
import os
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn
from dotenv import load_dotenv

load_dotenv(override=False)

logger = logging.getLogger(__name__)


def initialize_database():

    """Create the billing database and all tables from a clean schema."""



    database_url = os.getenv("DATABASE_URL")

    if not database_url:

        raise RuntimeError("DATABASE_URL is not configured in the .env file.")



    config = parse_dsn(database_url)

    database_name = config.get("dbname")

    if not database_name:

        raise RuntimeError("DATABASE_URL must include a database name.")



    # Create the target database if it does not exist.

    maintenance_config = config.copy()

    maintenance_config["options"] = "-c timezone=Asia/Kolkata"

    maintenance_config["dbname"] = "postgres"

    maintenance_connection = None



    try:

        maintenance_connection = psycopg2.connect(**maintenance_config)

        maintenance_connection.autocommit = True



        with maintenance_connection.cursor() as cursor:

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

        if maintenance_connection is not None:

            maintenance_connection.close()



    connection = None

    try:

        config["options"] = "-c timezone=Asia/Kolkata"
        connection = psycopg2.connect(**config)



        with connection:

            with connection.cursor() as cursor:

                # ==========================================================

                # PATIENTS

                # ==========================================================

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.patients (
                        id BIGSERIAL PRIMARY KEY,
                        patient_id VARCHAR(50) NOT NULL UNIQUE,
                        name VARCHAR(201) NOT NULL,
                        date_of_birth DATE,
                        gender VARCHAR(30),
                        mobile VARCHAR(20) NOT NULL,
                        email VARCHAR(255),
                        address TEXT NOT NULL,
                        pincode VARCHAR(10) NOT NULL,
                        referral VARCHAR(255),
                        source_of_lead VARCHAR(50),
                        registered_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)



               

                # ==========================================================

                # SERVICE GROUPS

                # ==========================================================

                cursor.execute("""

                    CREATE TABLE IF NOT EXISTS public.service_groups (

                        id BIGSERIAL PRIMARY KEY,

                        name VARCHAR(150) NOT NULL UNIQUE,

                        is_active BOOLEAN NOT NULL DEFAULT TRUE,

                        created_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP,

                        updated_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP

                    );

                """)



                # ==========================================================

                # SERVICES

                # ==========================================================

                cursor.execute("""

                    CREATE TABLE IF NOT EXISTS public.services (

                        id BIGSERIAL PRIMARY KEY,

                        service_group_id BIGINT NOT NULL

                            REFERENCES public.service_groups(id)

                            ON DELETE RESTRICT,

                        name VARCHAR(255) NOT NULL,

                        available_for_op BOOLEAN NOT NULL DEFAULT FALSE,

                        available_for_ip BOOLEAN NOT NULL DEFAULT FALSE,

                        is_active BOOLEAN NOT NULL DEFAULT TRUE,

                        created_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP,

                        updated_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP,

                        CONSTRAINT services_available_for_billing

                            CHECK (

                                available_for_op = TRUE

                                OR available_for_ip = TRUE

                            ),

                        CONSTRAINT uq_services_group_name

                            UNIQUE (service_group_id, name)

                    );

                """)



                # ==========================================================

                # ROOM TYPES

                # ==========================================================

                cursor.execute("""

                    CREATE TABLE IF NOT EXISTS public.room_types (

                        id BIGSERIAL PRIMARY KEY,

                        name VARCHAR(100) NOT NULL UNIQUE,

                        is_active BOOLEAN NOT NULL DEFAULT TRUE,

                        created_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP,

                        updated_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP

                    );

                """)



                # ==========================================================

                # DOCTORS

                # Psychologists also reference this table, using a separate

                # psychologist_id column on bills.

                # ==========================================================

                cursor.execute("""

                    CREATE TABLE IF NOT EXISTS public.doctors (

                        id BIGSERIAL PRIMARY KEY,

                        name VARCHAR(150) NOT NULL,

                        is_active BOOLEAN NOT NULL DEFAULT TRUE,

                        created_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP,

                        updated_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP

                    );

                """)



                # ==========================================================

                # BILL NUMBER AND PAYMENT RECEIPT SEQUENCES

                # ==========================================================

                cursor.execute("""
                    CREATE SEQUENCE IF NOT EXISTS public.bill_number_seq
                    AS BIGINT START WITH 1 INCREMENT BY 1;
                """)

                cursor.execute("""
                    CREATE SEQUENCE IF NOT EXISTS public.payment_refund_number_seq
                    AS BIGINT START WITH 1 INCREMENT BY 1;
                """)

                cursor.execute("""

                    CREATE SEQUENCE IF NOT EXISTS public.op_bill_number_seq

                    AS BIGINT START WITH 1 INCREMENT BY 1;

                """)

                cursor.execute("""

                    CREATE SEQUENCE IF NOT EXISTS public.ip_bill_number_seq

                    AS BIGINT START WITH 1 INCREMENT BY 1;

                """)

                cursor.execute("""

                    CREATE SEQUENCE IF NOT EXISTS public.bill_payment_receipt_seq

                    AS BIGINT START WITH 1 INCREMENT BY 1;

                """)



                # ==========================================================

                # ROLES

                # ==========================================================

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


                # ==========================================================
                # USERS
                # ==========================================================

                # login_id is entered by the administrator in the Create User
                # screen. Internal id remains database-generated and is used
                # for relationships and route parameters.
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.users (
                        id BIGSERIAL PRIMARY KEY,
                        login_id VARCHAR(20) NOT NULL,
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
                        CONSTRAINT uq_users_login_id UNIQUE (login_id),
                        CONSTRAINT users_full_name_not_blank
                            CHECK (LENGTH(TRIM(full_name)) > 0),
                        CONSTRAINT users_login_id_not_blank
                            CHECK (LENGTH(TRIM(login_id)) > 0)
                    );
                """)




                # ==========================================================

                # BILLS

                # Room type is required for IP and must be NULL for OP.

                # Names are snapshots; IDs are the foreign-key relationships.

                # ==========================================================

                cursor.execute("""

                    CREATE TABLE IF NOT EXISTS public.bills (

                        id BIGSERIAL PRIMARY KEY,

                        bill_number VARCHAR(100) NOT NULL UNIQUE,

                        bill_type VARCHAR(2) NOT NULL

                            CHECK (bill_type IN ('OP', 'IP')),

                        op_number VARCHAR(100) UNIQUE,

                        ip_number VARCHAR(100) UNIQUE,

                        patient_db_id BIGINT NOT NULL

                            REFERENCES public.patients(id)

                            ON DELETE RESTRICT,

                        
                        created_by BIGINT,

                        updated_by BIGINT,

                        updated_at TIMESTAMPTZ,

                        bill_date TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP,

                        department VARCHAR(100),



                        room_type_id BIGINT

                            REFERENCES public.room_types(id)

                            ON DELETE RESTRICT,

                        room_type_name VARCHAR(100),



                        doctor_id BIGINT

                            REFERENCES public.doctors(id)

                            ON DELETE RESTRICT,

                        doctor_name VARCHAR(150),



                        psychologist_id BIGINT

                            REFERENCES public.doctors(id)

                            ON DELETE RESTRICT,

                        psychologist_name VARCHAR(150),



                        admission_date TIMESTAMPTZ,

                        discharge_date TIMESTAMPTZ,

                        bed_number VARCHAR(100),

                        rate_category VARCHAR(100),




                        subtotal NUMERIC(12, 2) NOT NULL DEFAULT 0

                            CHECK (subtotal >= 0),

                        discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0

                            CHECK (discount_amount >= 0),

                        total_amount NUMERIC(12, 2) NOT NULL DEFAULT 0

                            CHECK (total_amount >= 0),

                        notes TEXT,

                        created_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP,



                        CONSTRAINT bills_number_matches_type CHECK (

                            (

                                bill_type = 'OP'

                                AND op_number IS NOT NULL

                                AND ip_number IS NULL

                                AND room_type_id IS NULL

                            )

                            OR

                            (

                                bill_type = 'IP'

                                AND ip_number IS NOT NULL

                                AND op_number IS NULL

                                AND room_type_id IS NOT NULL

                            )

                        )

                    );

                """)


            

                # ==========================================================

                # AUTOMATIC OP/IP BILL NUMBER GENERATION

                # ==========================================================

                cursor.execute("""
                    CREATE OR REPLACE FUNCTION public.set_bill_type_number()
                    RETURNS TRIGGER
                    LANGUAGE plpgsql
                    AS $$
                    BEGIN
                        -- Common sequence shared by OP and IP bills.
                        IF NEW.bill_number IS NULL OR BTRIM(NEW.bill_number) = '' THEN
                            NEW.bill_number :=
                                '2MGB-' || TO_CHAR(CURRENT_DATE, 'YYYYMMDD') || '-' ||
                                LPAD(NEXTVAL('public.bill_number_seq')::TEXT, 6, '0');
                        END IF;

                        IF NEW.bill_type = 'OP' THEN
                            IF NEW.op_number IS NULL OR BTRIM(NEW.op_number) = '' THEN
                                NEW.op_number :=
                                    '2MGOP-' || TO_CHAR(CURRENT_DATE, 'YYYYMMDD') || '-' ||
                                    LPAD(NEXTVAL('public.op_bill_number_seq')::TEXT, 6, '0');
                            END IF;
                            NEW.ip_number := NULL;

                        ELSIF NEW.bill_type = 'IP' THEN
                            IF NEW.ip_number IS NULL OR BTRIM(NEW.ip_number) = '' THEN
                                NEW.ip_number :=
                                    '2MGIP-' || TO_CHAR(CURRENT_DATE, 'YYYYMMDD') || '-' ||
                                    LPAD(NEXTVAL('public.ip_bill_number_seq')::TEXT, 6, '0');
                            END IF;
                            NEW.op_number := NULL;

                        ELSE
                            RAISE EXCEPTION 'Invalid bill_type: %', NEW.bill_type;
                        END IF;

                        RETURN NEW;
                    END;
                    $$;
                """)

                cursor.execute("""

                    DROP TRIGGER IF EXISTS trg_set_bill_type_number

                    ON public.bills;

                """)

                cursor.execute("""

                    CREATE TRIGGER trg_set_bill_type_number

                    BEFORE INSERT ON public.bills

                    FOR EACH ROW

                    EXECUTE FUNCTION public.set_bill_type_number();

                """)



                # ==========================================================

                # SERVICE PRICES

                # A row represents a price for a service and pricing context.

                # ==========================================================

                cursor.execute("""

                    CREATE TABLE IF NOT EXISTS public.service_prices (

                        id BIGSERIAL PRIMARY KEY,

                        service_id BIGINT NOT NULL

                            REFERENCES public.services(id)

                            ON DELETE RESTRICT,

                        bill_type VARCHAR(2) NOT NULL

                            CHECK (bill_type IN ('OP', 'IP')),

                        room_type_id BIGINT

                            REFERENCES public.room_types(id)

                            ON DELETE RESTRICT,

                        doctor_id BIGINT

                            REFERENCES public.doctors(id)

                            ON DELETE RESTRICT,

                        amount NUMERIC(12, 2) NOT NULL

                            CHECK (amount >= 0),

                        is_active BOOLEAN NOT NULL DEFAULT TRUE,

                        created_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP,

                        updated_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP,



                        CONSTRAINT service_prices_valid_conditions CHECK (

                            NOT (

                                room_type_id IS NOT NULL

                                AND doctor_id IS NOT NULL

                            )

                        ),

                        CONSTRAINT service_prices_room_ip_only CHECK (

                            room_type_id IS NULL OR bill_type = 'IP'

                        ),

                        CONSTRAINT service_prices_doctor_op_only CHECK (

                            doctor_id IS NULL OR bill_type = 'OP'

                        )

                    );

                """)



                # Prevent duplicate pricing combinations, including NULL

                # room/doctor values.

                cursor.execute("""

                    CREATE UNIQUE INDEX IF NOT EXISTS

                        uq_service_prices_conditions

                    ON public.service_prices (

                        service_id,

                        bill_type,

                        COALESCE(room_type_id, 0),

                        COALESCE(doctor_id, 0)

                    );

                """)



                # ==========================================================

                # BILL ITEMS

                # Service/group names and rates are snapshots for history.

                # IDs retain relationships to master/pricing records.

                # ==========================================================

                cursor.execute("""

                    CREATE TABLE IF NOT EXISTS public.bill_items (

                        id BIGSERIAL PRIMARY KEY,

                        bill_id BIGINT NOT NULL

                            REFERENCES public.bills(id)

                            ON DELETE CASCADE,

                        service_id BIGINT NOT NULL

                            REFERENCES public.services(id)

                            ON DELETE RESTRICT,

                        service_group_id BIGINT NOT NULL

                            REFERENCES public.service_groups(id)

                            ON DELETE RESTRICT,

                        service_price_id BIGINT

                            REFERENCES public.service_prices(id)

                            ON DELETE RESTRICT,



                        service_name VARCHAR(255) NOT NULL,

                        service_group_name VARCHAR(150) NOT NULL,

                        service_date DATE,

                        
                        room_type_id BIGINT
                            REFERENCES public.room_types(id)
                            ON DELETE RESTRICT,

                        room_type_name VARCHAR(100),

                        bed_number VARCHAR(100),

                        rate NUMERIC(12, 2) NOT NULL DEFAULT 0

                            CHECK (rate >= 0),

                        quantity NUMERIC(10, 2) NOT NULL DEFAULT 1

                            CHECK (quantity > 0),

                        amount NUMERIC(12, 2) NOT NULL DEFAULT 0

                            CHECK (amount >= 0),

                        discount_amount NUMERIC(12, 2) NOT NULL DEFAULT 0

                            CHECK (discount_amount >= 0),

                        gst_amount NUMERIC(12, 2) NOT NULL DEFAULT 0
                            CHECK (gst_amount >= 0),

                        net_amount NUMERIC(12, 2) NOT NULL DEFAULT 0

                            CHECK (net_amount >= 0),

                        created_at TIMESTAMPTZ NOT NULL

                            DEFAULT CURRENT_TIMESTAMP

                    );

                """)



                # ==========================================================

                # BILL PAYMENTS

                # ==========================================================

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.bill_payments (
                        id BIGSERIAL PRIMARY KEY,

                        bill_id BIGINT NOT NULL
                            REFERENCES public.bills(id)
                            ON DELETE RESTRICT,

                        receipt_number VARCHAR(30) NOT NULL UNIQUE
                            DEFAULT (
                                'RC-' || LPAD(
                                    NEXTVAL(
                                        'public.bill_payment_receipt_seq'
                                    )::TEXT,
                                    4,
                                    '0'
                                )
                            ),

                        amount NUMERIC(12, 2) NOT NULL
                            CHECK (amount > 0),

                        payment_date TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP,

                        payment_method VARCHAR(50) NOT NULL,
                        reference_number VARCHAR(150),
                        notes TEXT,

                        created_by BIGINT
                            REFERENCES public.users(id)
                            ON DELETE SET NULL,

                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)



                # ==========================================================
                # PAYMENT REFUNDS
                # ==========================================================

                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.payment_refunds (
                        id BIGSERIAL PRIMARY KEY,

                        refund_number VARCHAR(30) NOT NULL UNIQUE
                            DEFAULT (
                                'REF-' || LPAD(
                                    NEXTVAL(
                                        'public.payment_refund_number_seq'
                                    )::TEXT,
                                    6,
                                    '0'
                                )
                            ),

                        bill_id BIGINT NOT NULL
                            REFERENCES public.bills(id)
                            ON DELETE RESTRICT,

                        refund_amount NUMERIC(12, 2) NOT NULL
                            CHECK (refund_amount > 0),

                        refund_method VARCHAR(50) NOT NULL,

                        reason TEXT NOT NULL,

                        notes TEXT,

                        created_by BIGINT NOT NULL
                            REFERENCES public.users(id)
                            ON DELETE RESTRICT,

                        created_at TIMESTAMPTZ NOT NULL
                            DEFAULT CURRENT_TIMESTAMP
                    );
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_payment_refunds_bill_id
                    ON public.payment_refunds(bill_id);
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_payment_refunds_created_at
                    ON public.payment_refunds(created_at);
                """)



                # ==========================================================

                # INDEXES

                # ==========================================================

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_patients_name

                    ON public.patients(name);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_patients_mobile

                    ON public.patients(mobile);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_patients_registered_at

                    ON public.patients(registered_at);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_bills_patient_db_id

                    ON public.bills(patient_db_id);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_bills_bill_date

                    ON public.bills(bill_date);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_bills_bill_type

                    ON public.bills(bill_type);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_bill_items_bill_id

                    ON public.bill_items(bill_id);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_bill_items_service_id

                    ON public.bill_items(service_id);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_bill_items_service_group_id

                    ON public.bill_items(service_group_id);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_bill_payments_bill_id

                    ON public.bill_payments(bill_id);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_bill_payments_payment_date

                    ON public.bill_payments(payment_date);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_bill_payments_method

                    ON public.bill_payments(payment_method);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_services_group_id

                    ON public.services(service_group_id);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_service_prices_service_id

                    ON public.service_prices(service_id);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_service_prices_room_type

                    ON public.service_prices(room_type_id);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_service_prices_doctor

                    ON public.service_prices(doctor_id);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_users_role_id

                    ON public.users(role_id);

                """)

                cursor.execute("""

                    CREATE INDEX IF NOT EXISTS idx_users_is_active

                    ON public.users(is_active);

                """)

                # ==========================================================
                # BILL AUDIT HISTORY
                # ==========================================================
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.bill_audit_history (
                        id BIGSERIAL PRIMARY KEY,
                        bill_id BIGINT NOT NULL
                            REFERENCES public.bills(id) ON DELETE RESTRICT,
                        action VARCHAR(100) NOT NULL,
                        entity_type VARCHAR(50) NOT NULL,
                        entity_id BIGINT,
                        old_values JSONB,
                        new_values JSONB,
                        changed_by BIGINT,
                        changed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        reason TEXT
                    );
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_bill_audit_history_bill_changed_at
                    ON public.bill_audit_history(bill_id, changed_at DESC, id DESC);
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_bill_audit_history_entity
                    ON public.bill_audit_history(entity_type, entity_id);
                """)




                # ==========================================================
                # IP ROOM / BED ASSIGNMENT HISTORY
                # ==========================================================
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS public.ip_room_history (
                        id BIGSERIAL PRIMARY KEY,
                        bill_id BIGINT NOT NULL
                            REFERENCES public.bills(id) ON DELETE RESTRICT,
                        room_type_id BIGINT
                            REFERENCES public.room_types(id) ON DELETE SET NULL,
                        room_type_name VARCHAR(100),
                        bed_number VARCHAR(100),
                        start_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                        end_at TIMESTAMPTZ,
                        changed_by BIGINT,
                        change_reason TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
                        CONSTRAINT chk_ip_room_history_period
                            CHECK (end_at IS NULL OR end_at >= start_at)
                    );
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ip_room_history_bill_period
                    ON public.ip_room_history(bill_id, start_at DESC, id DESC);
                """)

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS idx_ip_room_history_active
                    ON public.ip_room_history(bill_id) WHERE end_at IS NULL;
                """)

        logger.info("Database initialization completed successfully.")



    except Exception:

        logger.exception("Database initialization failed.")

        raise

    finally:

        if connection is not None:

            connection.close()
