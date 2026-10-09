
import logging
import os
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv


# --------------------------------------------------
# Configuration
# --------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parent.parent

load_dotenv(BACKEND_DIR / ".env", override=False)

LOG_DIR = Path(
    os.getenv("LOG_DIR", str(BACKEND_DIR / "logs"))
)

LOG_FILE = LOG_DIR / "hms.log"

LOG_LEVEL_NAME = os.getenv(
    "LOG_LEVEL", "INFO"
).upper()

VALID_LEVELS = {
    "DEBUG",
    "INFO",
    "WARNING",
    "ERROR",
    "CRITICAL",
}

if LOG_LEVEL_NAME not in VALID_LEVELS:
    LOG_LEVEL_NAME = "INFO"

LOG_LEVEL = getattr(logging, LOG_LEVEL_NAME)

MAX_LOG_SIZE = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 10

LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | "
    "%(name)s | %(message)s"
)

DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


# --------------------------------------------------
# Formatter
# --------------------------------------------------

def create_formatter():
    return logging.Formatter(
        fmt=LOG_FORMAT,
        datefmt=DATE_FORMAT,
    )


# --------------------------------------------------
# Logger factory
# --------------------------------------------------

def get_logger(name: str) -> logging.Logger:
    """Return a consistently configured application logger."""

    logger = logging.getLogger(name)

    # Respect the configured application logging level.
    logger.setLevel(LOG_LEVEL)

    # Prevent the same record from being handled again
    # by the root logger or Uvicorn's parent handlers.
    logger.propagate = False

    # Avoid duplicate handlers on repeated imports.
    if getattr(logger, "_hms_configured", False):
        return logger

    formatter = create_formatter()

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(LOG_LEVEL)
    console_handler.setFormatter(formatter)

    logger.addHandler(console_handler)

    # File handler
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)

        file_handler = RotatingFileHandler(
            filename=LOG_FILE,
            maxBytes=MAX_LOG_SIZE,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )

        file_handler.setLevel(LOG_LEVEL)
        file_handler.setFormatter(formatter)

        logger.addHandler(file_handler)

    except OSError as exc:
        # Keep console logging available if file logging fails.
        logger.error(
            "Unable to initialize file logging: %s",
            exc,
        )

    logger._hms_configured = True

    return logger
