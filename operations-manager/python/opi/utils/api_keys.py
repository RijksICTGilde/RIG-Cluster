"""
API key generation utilities.

This module handles API key generation and encryption for projects.
"""

import logging
import secrets
import string

from opi.core.config import settings

logger = logging.getLogger(__name__)


class APIKeyEncryptionError(Exception):
    """Raised when API key encryption fails."""


class SOPSKeyNotAvailableError(Exception):
    """Raised when SOPS AGE key is not available for encryption."""


def generate_api_key(length: int = 32) -> str:
    """
    Generate an API key.

    Uses settings.API_TOKEN if USE_UNSAFE_API_KEY is True, otherwise generates random key.

    Args:
        length: Length of the API key (default: 32)

    Returns:
        Generated API key string
    """

    if settings.USE_UNSAFE_API_KEY:
        api_key = settings.API_TOKEN
        logger.debug(f"Using unsafe API key from settings: {api_key}")
    else:
        # Generate a secure random API key
        alphabet = string.ascii_letters + string.digits
        api_key = "".join(secrets.choice(alphabet) for _ in range(length))
        logger.debug(f"Generated secure random API key of length: {length}")

    return api_key
