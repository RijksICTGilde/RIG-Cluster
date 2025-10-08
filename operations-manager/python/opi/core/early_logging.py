"""
Early logging initialization module.

This module sets up logging before any other imports that might trigger logging.
It should be imported first in server.py to ensure logging is available during
config.py initialization.
"""

import logging
import os

from opi.utils.logging_config import setup_logging


def initialize_logging() -> None:
    """
    Initialize logging early in the application lifecycle.

    This function reads basic logging configuration from environment variables
    and sets up logging before other modules are imported.
    """
    # Get basic logging settings from environment
    log_to_file = os.environ.get("LOG_TO_FILE", "false").lower() == "true"
    log_file_path = os.environ.get("LOG_FILE_PATH", "log.txt")

    # Set up logging immediately
    setup_logging(log_to_file=log_to_file, log_file_path=log_file_path)

    # Get logger for this module
    logger = logging.getLogger(__name__)
    logger.debug("Early logging initialized successfully")


# Initialize logging when this module is imported
initialize_logging()
