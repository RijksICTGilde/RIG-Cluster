"""Logging, set up on import.

Imported first in server.py: anything imported after this may already log, and config.py
does so while it initialises.
"""

import logging
import os

from opi.utils.logging_config import setup_logging


def initialize_logging() -> None:
    """Reads its settings from the environment, not from ``settings``: config.py logs itself."""
    log_to_file = os.environ.get("LOG_TO_FILE", "false").lower() == "true"
    log_file_path = os.environ.get("LOG_FILE_PATH", "log.txt")
    log_errors_to_file = os.environ.get("LOG_ERRORS_TO_FILE", "false").lower() == "true"
    log_errors_file_path = os.environ.get("LOG_ERRORS_FILE_PATH", "/data/logs/errors.log")
    level = os.environ.get("LOG_LEVEL", "DEBUG")

    setup_logging(
        log_to_file=log_to_file,
        log_file_path=log_file_path,
        log_errors_to_file=log_errors_to_file,
        log_errors_file_path=log_errors_file_path,
        level=level,
    )

    logger = logging.getLogger(__name__)
    logger.debug("Early logging initialized successfully")


initialize_logging()
