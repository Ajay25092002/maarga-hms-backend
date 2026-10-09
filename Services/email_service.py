
import logging
import os
import smtplib

from pathlib import Path
from email.message import EmailMessage
from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BACKEND_DIR / ".env"

load_dotenv(dotenv_path=ENV_FILE, override=False)

logger = logging.getLogger(__name__)


def send_email(
    to_email: str,
    subject: str,
    body: str | None = None,
    html_body: str | None = None,
) -> bool:
    """Send a plain-text and/or HTML email using SMTP."""

    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    from_email = os.getenv("SMTP_FROM_EMAIL") or smtp_user

    if not smtp_user or not smtp_password or not from_email:
        raise ValueError("SMTP configuration is incomplete")

    if body is None and html_body is None:
        raise ValueError("Provide body or html_body")

    message = EmailMessage()
    message["From"] = from_email
    message["To"] = to_email
    message["Subject"] = subject

    message.set_content(
        body or "Please view this email in an HTML-capable email client."
    )

    if html_body is not None:
        message.add_alternative(html_body, subtype="html")

    try:
        with smtplib.SMTP(
            smtp_host,
            smtp_port,
            timeout=20,
        ) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(smtp_user, smtp_password)
            server.send_message(message)

        logger.info("Email sent successfully to %s", to_email)
        return True

    except (smtplib.SMTPException, OSError) as exc:
        logger.exception("SMTP email sending failed")
        raise RuntimeError("Failed to send email") from exc
