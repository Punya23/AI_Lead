"""
Structured JSON logging with loguru.

Every log line includes structured fields for easy grep-based debugging.
Lead-specific logs include lead_id as bound context.

Output format: JSON lines (one JSON object per log entry)
"""

import sys

from loguru import logger

from app.core.config import settings


def setup_logging() -> None:
    """Configure loguru for structured JSON logging.

    - Removes default stderr handler
    - Adds JSON-formatted handler to stdout
    - Sets log level based on DEBUG config

    Call this once at application startup (main.py).
    """
    # Remove default handler
    logger.remove()

    # JSON format for structured logging
    log_format = (
        '{{"timestamp":"{time:YYYY-MM-DDTHH:mm:ss.SSSZ}",'
        '"level":"{level.name}",'
        '"module":"{module}",'
        '"function":"{function}",'
        '"line":{line},'
        '"message":"{message}",'
        "{extra}"
        "}}"
    )

    # Add structured handler
    logger.add(
        sys.stdout,
        format=log_format,
        level="DEBUG" if settings.DEBUG else "INFO",
        serialize=True,  # Use loguru's built-in JSON serialization
        colorize=False,
        backtrace=True,
        diagnose=settings.DEBUG,
    )

    logger.info(
        "Logging initialized",
        app_name=settings.APP_NAME,
        environment=settings.APP_ENV,
        debug=settings.DEBUG,
    )


def get_lead_logger(lead_id: str):
    """Create a logger with lead_id bound as context.

    Args:
        lead_id: UUID string of the lead being processed.

    Returns:
        A loguru logger instance with lead_id in every log line.

    Usage:
        log = get_lead_logger("abc-123")
        log.info("Processing started", stage="enrichment")
    """
    return logger.bind(lead_id=lead_id)
