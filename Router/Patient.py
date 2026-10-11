import os

import re

import uuid

import logging



from datetime import date, datetime, time, timedelta, timezone

from typing import Optional



import psycopg2

from psycopg2 import IntegrityError, OperationalError

from psycopg2.extras import RealDictCursor



from dotenv import load_dotenv

from fastapi import APIRouter, HTTPException, Query, status

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator





load_dotenv()



logger = logging.getLogger(__name__)



router = APIRouter(

    prefix="/api/patients",

    tags=["Patients"],

)



CONNECTION_ENV_NAMES = (

    "DATABASE_URL",

    "DATABASE_CONNECTION_STRING",

    "POSTGRES_CONNECTION_STRING",

    "POSTGRES_URL",

)





# ---------------------------------------------------------

# Database connection

# ---------------------------------------------------------



def get_database_url() -> str:

    for env_name in CONNECTION_ENV_NAMES:

        value = os.getenv(env_name)

        if value and value.strip():

            return value.strip()



    raise RuntimeError(

        "PostgreSQL connection string is not configured."

    )





def get_db_connection():

    return psycopg2.connect(

        get_database_url(),

        connect_timeout=10,

        cursor_factory=RealDictCursor,

    )





# ---------------------------------------------------------

# Request validation

# ---------------------------------------------------------



class PatientCreate(BaseModel):

    model_config = ConfigDict(str_strip_whitespace=True)



    name: str = Field(min_length=1, max_length=201)

    date_of_birth: Optional[date] = None

    gender: Optional[str] = Field(default=None, max_length=30)

    mobile: str = Field(min_length=5, max_length=25)

    email: Optional[EmailStr] = None

    address: Optional[str] = Field(default=None, max_length=1000)

    registration_number: Optional[str] = Field(

        default=None,

        min_length=1,

        max_length=100,

    )



    @field_validator("name")

    @classmethod

    def validate_name(cls, value):

        value = value.strip()

        if not value:

            raise ValueError("Patient name cannot be empty.")

        return value



    @field_validator("mobile")

    @classmethod

    def validate_mobile(cls, value):

        value = value.strip()

        if not re.fullmatch(r"[0-9+() .-]{5,25}", value):

            raise ValueError("Enter a valid mobile number.")

        return value



    @field_validator("gender")

    @classmethod

    def validate_gender(cls, value):

        if value is None:

            return value



        allowed = {

            "male",

            "female",

            "other",

            "prefer not to say",

        }



        value = value.strip().lower()



        if value not in allowed:

            raise ValueError("Invalid gender value.")



        return value





class PatientUpdate(BaseModel):

    model_config = ConfigDict(str_strip_whitespace=True)



    name: str = Field(min_length=1, max_length=201)

    date_of_birth: Optional[date] = None

    gender: Optional[str] = Field(default=None, max_length=30)

    mobile: str = Field(min_length=5, max_length=25)

    email: Optional[EmailStr] = None

    address: Optional[str] = Field(default=None, max_length=1000)

    registration_number: Optional[str] = Field(

        default=None,

        max_length=100,

    )



    @field_validator("name")

    @classmethod

    def validate_name(cls, value):

        value = value.strip()

        if not value:

            raise ValueError("Patient name cannot be empty.")

        return value



    @field_validator("mobile")

    @classmethod

    def validate_mobile(cls, value):

        value = value.strip()

        if not re.fullmatch(r"[0-9+() .-]{5,25}", value):

            raise ValueError("Enter a valid mobile number.")

        return value



    @field_validator("gender")

    @classmethod

    def validate_gender(cls, value):

        if value is None:

            return value



        allowed = {

            "male",

            "female",

            "other",

            "prefer not to say",

        }



        value = value.strip().lower()



        if value not in allowed:

            raise ValueError("Invalid gender value.")



        return value





# ---------------------------------------------------------

# Schema validation

# ---------------------------------------------------------



EXPECTED_COLUMNS = {

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





def validate_patient_schema(connection):

    with connection.cursor() as cursor:

        cursor.execute(

            """

            SELECT column_name

            FROM information_schema.columns

            WHERE table_schema = 'public'

              AND table_name = 'patients'

            """

        )



        existing_columns = {

            row["column_name"] for row in cursor.fetchall()

        }



    missing = EXPECTED_COLUMNS - existing_columns



    if missing:

        raise RuntimeError(

            "The patients table is missing required columns: "

            + ", ".join(sorted(missing))

        )





def initialize_patient_table():

    """

    Validate the existing patients table.



    This function does not drop or recreate existing tables.

    """

    connection = None



    try:

        connection = get_db_connection()

        validate_patient_schema(connection)

        logger.info("Patient database schema validated.")



    except Exception:

        logger.exception("Patient schema validation failed.")

        raise



    finally:

        if connection is not None:

            connection.close()





# ---------------------------------------------------------

# Helpers

# ---------------------------------------------------------



def serialize_patient(patient: dict) -> dict:

    return {

        key: (

            value.isoformat()

            if isinstance(value, (date, datetime))

            else value

        )

        for key, value in patient.items()

    }





def build_filters(

    search: Optional[str],

    gender: Optional[str],

    registration_date: str,

):

    conditions = []

    params = []



    if search and search.strip():

        term = f"%{search.strip()}%"



        conditions.append(

            """

            (

                name ILIKE %s

                OR patient_id ILIKE %s

                OR COALESCE(registration_number, '') ILIKE %s

                OR mobile ILIKE %s

            )

            """

        )



        params.extend([term, term, term, term])



    if gender:

        conditions.append("LOWER(gender) = LOWER(%s)")

        params.append(gender.strip())



    now = datetime.now(timezone.utc)



    if registration_date == "today":

        start = datetime.combine(

            now.date(),

            time.min,

            tzinfo=timezone.utc,

        )

        end = start + timedelta(days=1)



        conditions.append(

            "registered_at >= %s AND registered_at < %s"

        )

        params.extend([start, end])



    elif registration_date == "last_7_days":

        start_date = now.date() - timedelta(days=6)



        start = datetime.combine(

            start_date,

            time.min,

            tzinfo=timezone.utc,

        )



        conditions.append("registered_at >= %s AND registered_at <= %s")

        params.extend([start, now])



    where_clause = (

        "WHERE " + " AND ".join(conditions)

        if conditions

        else ""

    )



    return where_clause, params





def fetch_one_patient(patient_id: str):

    connection = get_db_connection()



    try:

        with connection.cursor() as cursor:

            cursor.execute(

                """

                SELECT

                    id,

                    patient_id,

                    registration_number,

                    name,

                    date_of_birth,

                    gender,

                    mobile,

                    email,

                    address,

                    registered_at

                FROM public.patients

                WHERE patient_id = %s

                """,

                (patient_id,),

            )



            return cursor.fetchone()



    finally:

        connection.close()





# ---------------------------------------------------------

# API: Register a patient

# ---------------------------------------------------------



@router.post("", status_code=status.HTTP_201_CREATED)

def register_patient(payload: PatientCreate):

    patient_data = payload.model_dump()
    patient_id = f"PAT-{uuid.uuid4().hex[:12].upper()}"
    supplied_registration_number = patient_data.get("registration_number")
    registration_number = (
        supplied_registration_number.strip()
        if supplied_registration_number and supplied_registration_number.strip()
        else patient_id
    )

    connection = None



    try:

        connection = get_db_connection()



        with connection:

            with connection.cursor() as cursor:

                cursor.execute(

                    """

                    INSERT INTO public.patients (

                        patient_id,

                        registration_number,

                        name,

                        date_of_birth,

                        gender,

                        mobile,

                        email,

                        address

                    )

                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)

                    RETURNING

                        id,

                        patient_id,

                        registration_number,

                        name,

                        date_of_birth,

                        gender,

                        mobile,

                        email,

                        address,

                        registered_at

                    """,

                    (

                        patient_id,

                        registration_number,

                        patient_data["name"],

                        patient_data["date_of_birth"],

                        patient_data["gender"],

                        patient_data["mobile"],

                        (

                            str(patient_data["email"])

                            if patient_data["email"]

                            else None

                        ),

                        patient_data["address"],

                    ),

                )



                patient = cursor.fetchone()



        return serialize_patient(dict(patient))



    except IntegrityError as exc:

        if connection is not None:

            connection.rollback()



        constraint = getattr(

            getattr(exc, "diag", None),

            "constraint_name",

            "",

        )



        if constraint and "registration_number" in constraint:

            raise HTTPException(

                status_code=status.HTTP_409_CONFLICT,

                detail="Registration number already exists.",

            ) from exc



        raise HTTPException(

            status_code=status.HTTP_409_CONFLICT,

            detail="A duplicate patient identifier or registration number exists.",

        ) from exc



    except OperationalError as exc:

        logger.exception("Patient registration database connection failed.")

        raise HTTPException(

            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,

            detail="Patient registration is temporarily unavailable.",

        ) from exc



    except HTTPException:

        raise



    except Exception as exc:

        logger.exception("Patient registration failed.")

        raise HTTPException(

            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,

            detail="Unable to register patient.",

        ) from exc



    finally:

        if connection is not None:

            connection.close()





# ---------------------------------------------------------

# API: List, search, filter and paginate patients

# ---------------------------------------------------------



@router.get("")

def list_patients(

    search: Optional[str] = Query(default=None, max_length=200),

    gender: Optional[str] = Query(default=None, max_length=30),

    registration_date: str = Query(

        default="all",

        pattern="^(all|today|last_7_days)$",

    ),

    page: int = Query(default=1, ge=1),

    page_size: int = Query(default=10, ge=1, le=100),

):

    where_clause, params = build_filters(

        search,

        gender,

        registration_date,

    )



    offset = (page - 1) * page_size

    connection = None



    try:

        connection = get_db_connection()



        with connection.cursor() as cursor:

            cursor.execute(

                f"""

                SELECT COUNT(*) AS total

                FROM public.patients

                {where_clause}

                """,

                params,

            )



            total = cursor.fetchone()["total"]



            cursor.execute(

                f"""

                SELECT

                    id,

                    patient_id,

                    registration_number,

                    name,

                    date_of_birth,

                    gender,

                    mobile,

                    email,

                    address,

                    registered_at

                FROM public.patients

                {where_clause}

                ORDER BY registered_at DESC, id DESC

                LIMIT %s OFFSET %s

                """,

                params + [page_size, offset],

            )



            items = [

                serialize_patient(dict(row))

                for row in cursor.fetchall()

            ]



        return {

            "items": items,

            "total": total,

            "page": page,

            "page_size": page_size,

            "total_pages": (total + page_size - 1) // page_size,

        }



    except OperationalError as exc:

        logger.exception("Unable to connect while listing patients.")

        raise HTTPException(

            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,

            detail="Patient listing is temporarily unavailable.",

        ) from exc



    except Exception as exc:

        logger.exception("Unable to list patients.")

        raise HTTPException(

            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,

            detail="Unable to retrieve patients.",

        ) from exc



    finally:

        if connection is not None:

            connection.close()





# ---------------------------------------------------------

# API: Update a patient

# ---------------------------------------------------------



@router.put("/{patient_id}")

def update_patient(patient_id: str, payload: PatientUpdate):

    patient_data = payload.model_dump()

    connection = None



    try:

        connection = get_db_connection()



        with connection:

            with connection.cursor() as cursor:

                cursor.execute(

                    """

                    UPDATE public.patients

                    SET

                        registration_number = %s,

                        name = %s,

                        date_of_birth = %s,

                        gender = %s,

                        mobile = %s,

                        email = %s,

                        address = %s

                    WHERE patient_id = %s

                    RETURNING

                        id,

                        patient_id,

                        registration_number,

                        name,

                        date_of_birth,

                        gender,

                        mobile,

                        email,

                        address,

                        registered_at

                    """,

                    (

                        patient_data["registration_number"],

                        patient_data["name"],

                        patient_data["date_of_birth"],

                        patient_data["gender"],

                        patient_data["mobile"],

                        (

                            str(patient_data["email"])

                            if patient_data["email"]

                            else None

                        ),

                        patient_data["address"],

                        patient_id,

                    ),

                )



                patient = cursor.fetchone()



                if patient is None:

                    raise HTTPException(

                        status_code=status.HTTP_404_NOT_FOUND,

                        detail="Patient not found.",

                    )



        return serialize_patient(dict(patient))



    except IntegrityError as exc:

        if connection is not None:

            connection.rollback()



        raise HTTPException(

            status_code=status.HTTP_409_CONFLICT,

            detail="The registration number is already assigned to another patient.",

        ) from exc



    except OperationalError as exc:

        logger.exception("Patient update database connection failed.")

        raise HTTPException(

            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,

            detail="Patient update is temporarily unavailable.",

        ) from exc



    except HTTPException:

        raise



    except Exception as exc:

        logger.exception("Patient update failed.")

        raise HTTPException(

            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,

            detail="Unable to update patient.",

        ) from exc



    finally:

        if connection is not None:

            connection.close()





# ---------------------------------------------------------

# API: Get one patient by patient ID / UHID

# ---------------------------------------------------------



@router.get("/{patient_id}")

def get_patient(patient_id: str):

    try:

        patient = fetch_one_patient(patient_id)



        if patient is None:

            raise HTTPException(

                status_code=status.HTTP_404_NOT_FOUND,

                detail="Patient not found.",

            )



        return serialize_patient(dict(patient))



    except HTTPException:

        raise



    except Exception as exc:
        logger.exception("Unable to retrieve patient.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unable to retrieve patient details.",
        ) from exc


# ---------------------------------------------------------

# API: Patient dashboard summary metrics

# ---------------------------------------------------------

@router.get("/summary/metrics")

def patient_summary():
    connection = None
    try:
        connection = get_db_connection()
        with connection.cursor() as cursor:

            cursor.execute(

                """
                SELECT

                    COUNT(*) AS total_patients,

                    COUNT(*) FILTER (

                        WHERE registered_at >= date_trunc('day', NOW())

                          AND registered_at <

                              date_trunc('day', NOW()) + INTERVAL '1 day'

                    ) AS registered_today,

                    COUNT(*) FILTER (

                        WHERE registered_at >= NOW() - INTERVAL '7 days'

                    ) AS recently_added

                FROM public.patients

                """

            )



            return dict(cursor.fetchone())



    except Exception as exc:

        logger.exception("Unable to retrieve patient metrics.")

        raise HTTPException(

            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,

            detail="Unable to retrieve patient metrics.",

        ) from exc



    finally:

        if connection is not None:

            connection.close()