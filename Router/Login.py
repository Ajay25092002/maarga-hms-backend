
import hashlib
import json
from Core.logger import get_logger
import os
import secrets
from pathlib import Path

import redis
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

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

logger = get_logger(__name__)

# --------------------------------------------------
# Router
# --------------------------------------------------

router = APIRouter(
    prefix="/api/auth",
    tags=["Authentication"],
)


# --------------------------------------------------
# OTP configuration
# --------------------------------------------------

OTP_EXPIRY_SECONDS = 300
MAX_OTP_ATTEMPTS = 5


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


# --------------------------------------------------
# Redis connection
# --------------------------------------------------

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
# Redis key helpers
# --------------------------------------------------

def get_otp_keys(email: str) -> tuple[str, str]:
    """Generate Redis OTP keys without exposing email addresses."""
    email_hash = hashlib.sha256(
        email.encode("utf-8")
    ).hexdigest()

    return (
        f"auth:otp:{email_hash}",
        f"auth:otp_attempts:{email_hash}",
    )


def get_session_key(session_id: str) -> str:
    """Hash the session ID before using it as a Redis key."""
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
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


# --------------------------------------------------
# Login API
# POST /api/auth/login
# --------------------------------------------------

@router.post("/login")
def login(data: LoginRequest, response: Response):
    """Validate development credentials and create a session."""

    expected_email = os.getenv("DEV_LOGIN_EMAIL")
    expected_password = os.getenv("DEV_LOGIN_PASSWORD")

    if not expected_email or not expected_password:
        logger.error("Development login credentials are not configured.")

        raise HTTPException(
            status_code=500,
            detail="Login credentials are not configured.",
        )

    email = str(data.email).strip().lower()
    configured_email = expected_email.strip().lower()

    email_matches = secrets.compare_digest(
        email,
        configured_email,
    )

    password_matches = secrets.compare_digest(
        data.password,
        expected_password,
    )

    if not (email_matches and password_matches):
        raise HTTPException(
            status_code=401,
            detail="Invalid email or password.",
        )

    session_id = secrets.token_urlsafe(32)

    session_data = {
        "email": configured_email,
    }

    try:
        redis_client.setex(
            get_session_key(session_id),
            SESSION_TTL_SECONDS,
            json.dumps(session_data),
        )

    except redis.exceptions.RedisError:
        logger.exception("Unable to create login session.")

        raise HTTPException(
            status_code=503,
            detail="Session service is temporarily unavailable.",
        ) from None

    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
        path="/",
    )

    return {
        "message": "Login successful.",
        "user": session_data,
    }


# --------------------------------------------------
# Current user API
# GET /api/auth/me
# --------------------------------------------------

@router.get("/me")
def get_current_user(request: Request):
    """Return the current user if the session is valid."""

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
        logger.exception("Unable to validate login session.")

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
        user = json.loads(session_data)

    except (TypeError, json.JSONDecodeError):
        logger.error("Invalid session data found in Redis.")

        raise HTTPException(
            status_code=401,
            detail="Invalid session. Please log in again.",
        ) from None

    return {
        "authenticated": True,
        "user": user,
    }


# --------------------------------------------------
# Logout API
# POST /api/auth/logout
# --------------------------------------------------

@router.post("/logout")
def logout(request: Request, response: Response):
    """Delete the Redis session and clear the cookie."""

    session_id = request.cookies.get(
        SESSION_COOKIE_NAME
    )

    if session_id:
        try:
            redis_client.delete(
                get_session_key(session_id)
            )

        except redis.exceptions.RedisError:
            logger.exception("Unable to delete login session.")

            raise HTTPException(
                status_code=503,
                detail="Could not end the session. Please try again.",
            ) from None

    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        httponly=True,
        secure=COOKIE_SECURE,
        samesite="lax",
    )

    return {
        "message": "Logged out successfully."
    }


# --------------------------------------------------
# Send OTP API
# POST /api/auth/send-otp
# --------------------------------------------------

@router.post("/send-otp")
def send_otp(request: SendOTPRequest):
    email = str(request.email).lower()
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
        logger.exception("Failed to send or store login OTP")

        raise HTTPException(
            status_code=503,
            detail="Unable to process the verification request.",
        ) from None

    except Exception:
        logger.exception(
            "Unexpected error while sending login OTP"
        )

        raise HTTPException(
            status_code=500,
            detail="Unable to process the verification request.",
        ) from None


# --------------------------------------------------
# Verify OTP API
# POST /api/auth/verify-otp
# --------------------------------------------------

@router.post("/verify-otp")
def verify_otp(request: VerifyOTPRequest):
    email = str(request.email).lower()
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
            "message": "OTP verified successfully."
        }

    except HTTPException:
        raise

    except redis.exceptions.RedisError:
        logger.exception(
            "Redis error during OTP verification"
        )

        raise HTTPException(
            status_code=503,
            detail=(
                "Verification service is temporarily unavailable."
            ),
        ) from None


# --------------------------------------------------
# Redis cleanup
# --------------------------------------------------

@router.on_event("shutdown")
def close_redis_connection():
    redis_client.close()
