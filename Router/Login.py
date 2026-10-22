
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
from pathlib import Path

import psycopg2
import redis
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from psycopg2.extras import RealDictCursor

from Schemas.role_schema import normalize_permissions
from Services.email_service import send_email
from Services.template_service import render_email_template


# --------------------------------------------------
# Environment configuration
# --------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent

load_dotenv(
    dotenv_path=BACKEND_DIR / ".env",
    override=False,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/auth",
    tags=["Authentication"],
)


# --------------------------------------------------
# Session configuration
# --------------------------------------------------

SESSION_COOKIE_NAME = "hms_session"

SESSION_TTL_SECONDS = int(
    os.getenv("SESSION_TTL_SECONDS", "28800")
)

COOKIE_SECURE = (
    os.getenv("COOKIE_SECURE", "false").lower() == "true"
)

REDIS_URL = os.getenv(
    "REDIS_URL",
    "redis://localhost:6379/0",
)

redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=5,
    socket_timeout=5,
)


# --------------------------------------------------
# OTP configuration
# --------------------------------------------------

OTP_EXPIRY_SECONDS = 300
MAX_OTP_ATTEMPTS = 5


# --------------------------------------------------
# Database connection
# --------------------------------------------------

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


# --------------------------------------------------
# Password verification
# Compatible with Services/user_service.py
# --------------------------------------------------

def verify_password(password: str, stored_hash: str) -> bool:
    try:
        (
            algorithm,
            n,
            r,
            p,
            salt_text,
            hash_text,
        ) = stored_hash.split("$")

        if algorithm != "scrypt":
            return False

        salt = base64.b64decode(
            salt_text,
            validate=True,
        )

        expected_hash = base64.b64decode(
            hash_text,
            validate=True,
        )

        actual_hash = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected_hash),
        )

        return hmac.compare_digest(
            actual_hash,
            expected_hash,
        )

    except (ValueError, TypeError):
        return False


# --------------------------------------------------
# Redis key helpers
# --------------------------------------------------

def get_otp_keys(email: str) -> tuple[str, str]:
    email_hash = hashlib.sha256(
        email.encode("utf-8")
    ).hexdigest()

    return (
        f"auth:otp:{email_hash}",
        f"auth:otp_attempts:{email_hash}",
    )


def get_session_key(session_id: str) -> str:
    session_hash = hashlib.sha256(
        session_id.encode("utf-8")
    ).hexdigest()

    return f"auth:session:{session_hash}"


# --------------------------------------------------
# Request models
# --------------------------------------------------

class SendOTPRequest(BaseModel):
    email: EmailStr
    name: str = "User"


class VerifyOTPRequest(BaseModel):
    email: EmailStr
    otp: str = Field(pattern=r"^\d{6}$")


class LoginRequest(BaseModel):
    login_id: str = Field(min_length=4, max_length=20)
    password: str = Field(min_length=1, max_length=256)


# --------------------------------------------------
# Shared authentication helpers
# --------------------------------------------------

def get_user_by_login_id(login_id: str):
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    u.id,
                    u.login_id,
                    u.full_name,
                    u.email,
                    u.password_hash,
                    u.role_id,
                    u.is_active AS user_is_active,
                    r.name AS role_name,
                    r.is_active AS role_is_active,
                    r.permissions
                FROM public.users u
                JOIN public.roles r
                    ON r.id = u.role_id
                WHERE upper(trim(u.login_id)) = %s
                LIMIT 1
                """,
                (login_id.upper(),)
            )

            return cursor.fetchone()

    finally:
        if connection is not None:
            connection.close()


def get_user_by_id(user_id: int):
    connection = None

    try:
        connection = get_db_connection()

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    u.id,
                    u.login_id,
                    u.full_name,
                    u.email,
                    u.role_id,
                    u.is_active AS user_is_active,
                    r.name AS role_name,
                    r.is_active AS role_is_active,
                    r.permissions
                FROM public.users u
                JOIN public.roles r
                    ON r.id = u.role_id
                WHERE u.id = %s
                LIMIT 1
                """,
                (user_id,),
            )

            return cursor.fetchone()

    finally:
        if connection is not None:
            connection.close()


def serialize_authenticated_user(row: dict) -> dict:
    permissions = row.get("permissions") or {}

    # PostgreSQL JSONB is normally returned as a Python dictionary.
    # Handle serialized JSON too, if the database driver returns a string.
    if isinstance(permissions, str):
        try:
            permissions = json.loads(permissions)
        except json.JSONDecodeError:
            permissions = {}

    if not isinstance(permissions, dict):
        permissions = {}

    permissions = normalize_permissions(permissions)

    return {
        "id": row["id"],
        "login_id": row.get("login_id"),
        "full_name": row["full_name"],
        "email": row["email"],
        "role_id": row["role_id"],
        "role_name": row["role_name"],
        "permissions": permissions,
    }


def clear_session_cookie(response: Response):
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )


def create_session_cookie(
    response: Response,
    session_id: str,
):
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


# --------------------------------------------------
# Login
# POST /api/auth/login
# --------------------------------------------------

@router.post("/login")
def login(data: LoginRequest, response: Response):
    login_id = data.login_id.strip().upper()

    try:
        user = get_user_by_login_id(login_id)

    except (psycopg2.Error, RuntimeError):
        logger.exception(
            "Database error during authentication."
        )

        raise HTTPException(
            status_code=503,
            detail="Authentication service is temporarily unavailable.",
        ) from None

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Invalid User ID or password.",
        )

    stored_hash = user.get("password_hash") or ""

    try:
        password_matches = verify_password(
            data.password,
            stored_hash,
        )
    except (ValueError, MemoryError):
        logger.exception(
            "Password verification failed unexpectedly."
        )
        password_matches = False

    if not password_matches:
        raise HTTPException(
            status_code=401,
            detail="Invalid User ID or password.",
        )

    if not user["user_is_active"]:
        raise HTTPException(
            status_code=403,
            detail="Your account is inactive. Contact your administrator.",
        )

    if not user["role_is_active"]:
        raise HTTPException(
            status_code=403,
            detail="Your assigned role is inactive. Contact your administrator.",
        )

    authenticated_user = serialize_authenticated_user(user)

    # Store only the user ID in the session.
    # Name, role, and permissions are retrieved from the database
    # whenever /api/auth/me is called.
    session_data = {
        "user_id": authenticated_user["id"],
    }

    session_id = secrets.token_urlsafe(32)

    try:
        redis_client.setex(
            get_session_key(session_id),
            SESSION_TTL_SECONDS,
            json.dumps(session_data),
        )

    except redis.exceptions.RedisError:
        logger.exception(
            "Unable to create authentication session."
        )

        raise HTTPException(
            status_code=503,
            detail="Session service is temporarily unavailable.",
        ) from None

    create_session_cookie(response, session_id)

    logger.info(
        "User logged in successfully: user_id=%s",
        authenticated_user["id"],
    )

    return {
        "message": "Login successful.",
        "user": authenticated_user,
    }


# --------------------------------------------------
# Current authenticated user
# GET /api/auth/me
# --------------------------------------------------

@router.get("/me")
def get_current_user(request: Request):
    session_id = request.cookies.get(
        SESSION_COOKIE_NAME
    )

    if not session_id:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated.",
        )

    try:
        session_data = redis_client.get(
            get_session_key(session_id)
        )

    except redis.exceptions.RedisError:
        logger.exception(
            "Unable to validate authentication session."
        )

        raise HTTPException(
            status_code=503,
            detail="Session service is temporarily unavailable.",
        ) from None

    if not session_data:
        raise HTTPException(
            status_code=401,
            detail="Session expired or invalid. Please log in again.",
        )

    try:
        session = json.loads(session_data)
        user_id = int(session["user_id"])

    except (
        TypeError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
    ):
        logger.warning(
            "Invalid authentication session data."
        )

        raise HTTPException(
            status_code=401,
            detail="Invalid session. Please log in again.",
        ) from None

    try:
        user = get_user_by_id(user_id)

    except (psycopg2.Error, RuntimeError):
        logger.exception(
            "Unable to load current authenticated user."
        )

        raise HTTPException(
            status_code=503,
            detail="Authentication service is temporarily unavailable.",
        ) from None

    if user is None:
        raise HTTPException(
            status_code=401,
            detail="Your account no longer exists. Please log in again.",
        )

    if not user["user_is_active"]:
        raise HTTPException(
            status_code=403,
            detail="Your account is inactive.",
        )

    if not user["role_is_active"]:
        raise HTTPException(
            status_code=403,
            detail="Your assigned role is inactive.",
        )

    return {
        "authenticated": True,
        "user": serialize_authenticated_user(user),
    }


# --------------------------------------------------
# Logout
# POST /api/auth/logout
# --------------------------------------------------

@router.post("/logout")
def logout(
    request: Request,
    response: Response,
):
    session_id = request.cookies.get(
        SESSION_COOKIE_NAME
    )

    if session_id:
        try:
            redis_client.delete(
                get_session_key(session_id)
            )

        except redis.exceptions.RedisError:
            logger.exception(
                "Unable to delete authentication session."
            )

            raise HTTPException(
                status_code=503,
                detail="Could not end the session. Please try again.",
            ) from None

    clear_session_cookie(response)

    return {
        "message": "Logged out successfully.",
    }


# --------------------------------------------------
# Send OTP
# POST /api/auth/send-otp
# --------------------------------------------------

@router.post("/send-otp")
def send_otp(request: SendOTPRequest):
    email = str(request.email).strip().lower()
    name = request.name.strip() or "User"

    try:
        redis_client.ping()

        otp = f"{secrets.randbelow(1_000_000):06d}"

        html_content = render_email_template(
            "login_otp.html",
            name=name,
            otp=otp,
            expiry_minutes=OTP_EXPIRY_SECONDS // 60,
        )

        send_email(
            to_email=email,
            subject="Maarga Mindcare - Login Verification Code",
            body=(
                f"Hello {name},\n\n"
                f"Your login verification code is {otp}.\n"
                "It expires in 5 minutes. Do not share this code."
            ),
            html_body=html_content,
        )

        otp_key, attempts_key = get_otp_keys(email)

        with redis_client.pipeline(transaction=True) as pipe:
            pipe.set(
                otp_key,
                otp,
                ex=OTP_EXPIRY_SECONDS,
            )

            pipe.set(
                attempts_key,
                0,
                ex=OTP_EXPIRY_SECONDS,
            )

            pipe.execute()

        return {
            "message": (
                "If the address is eligible, "
                "a verification email has been sent."
            )
        }

    except (
        redis.exceptions.RedisError,
        RuntimeError,
        ValueError,
        OSError,
    ):
        logger.exception(
            "Failed to send or store login OTP."
        )

        raise HTTPException(
            status_code=503,
            detail="Unable to process the verification request.",
        ) from None

    except Exception:
        logger.exception(
            "Unexpected error while sending login OTP."
        )

        raise HTTPException(
            status_code=500,
            detail="Unable to process the verification request.",
        ) from None


# --------------------------------------------------
# Verify OTP
# POST /api/auth/verify-otp
# --------------------------------------------------

@router.post("/verify-otp")
def verify_otp(request: VerifyOTPRequest):
    email = str(request.email).strip().lower()
    otp_key, attempts_key = get_otp_keys(email)

    try:
        stored_otp = redis_client.get(otp_key)

        if stored_otp is None:
            raise HTTPException(
                status_code=400,
                detail="Invalid or expired OTP.",
            )

        attempts = int(
            redis_client.get(attempts_key) or 0
        )

        if attempts >= MAX_OTP_ATTEMPTS:
            redis_client.delete(
                otp_key,
                attempts_key,
            )

            raise HTTPException(
                status_code=429,
                detail=(
                    "Too many verification attempts. "
                    "Request a new OTP."
                ),
            )

        if not secrets.compare_digest(
            stored_otp,
            request.otp,
        ):
            redis_client.incr(attempts_key)

            raise HTTPException(
                status_code=400,
                detail="Invalid or expired OTP.",
            )

        # OTP can only be used once.
        redis_client.delete(
            otp_key,
            attempts_key,
        )

        return {
            "message": "OTP verified successfully.",
        }

    except HTTPException:
        raise

    except redis.exceptions.RedisError:
        logger.exception(
            "Redis error during OTP verification."
        )

        raise HTTPException(
            status_code=503,
            detail="Verification service is temporarily unavailable.",
        ) from None


# --------------------------------------------------
# Redis cleanup
# --------------------------------------------------

@router.on_event("shutdown")
def close_redis_connection():
    redis_client.close()
