"""Generating an API key, with the unsafe fixed-key escape hatch for local development."""

import logging
import secrets
import string

from opi.core.config import settings

logger = logging.getLogger(__name__)


class SOPSKeyNotAvailableError(Exception):
    """Raised when SOPS AGE key is not available for encryption."""


def generate_api_key(length: int = 32) -> str:
    """A random API key, or ``settings.API_TOKEN`` when ``USE_UNSAFE_API_KEY`` is set."""

    if settings.USE_UNSAFE_API_KEY:
        api_key = settings.API_TOKEN
        logger.debug("Using unsafe API key from settings")
    else:
        alphabet = string.ascii_letters + string.digits
        api_key = "".join(secrets.choice(alphabet) for _ in range(length))
        logger.debug(f"Generated secure random API key of length: {length}")

    return api_key
