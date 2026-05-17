"""
SECRET_KEY safety validation.

SECRET_KEY signs the session cookie (Starlette SessionMiddleware) and the
websocket log-stream session. A weak or publicly known key lets an attacker
forge a session for an arbitrary user, bypassing both HTTP and websocket auth.

This module holds the pure validation logic with no pydantic dependency so it
can be unit-tested in isolation and reused by the Settings model validator.
"""

import logging

logger = logging.getLogger(__name__)

# Well-known development default for SECRET_KEY. This value is committed to the
# public repository, so anyone can forge a signed session cookie when it is used.
# It is only acceptable in explicit development/DEBUG mode.
DEV_DEFAULT_SECRET_KEY = "default-secret-key-for-development-change-in-production"

# Minimum acceptable SECRET_KEY length for production. itsdangerous accepts any
# non-empty key, so we enforce a floor that makes brute-forcing infeasible.
MIN_SECRET_KEY_LENGTH = 32


class InsecureSecretKeyError(RuntimeError):
    """Raised at startup when SECRET_KEY is unsafe for a production-like deployment."""


def validate_secret_key(secret_key: str | None, debug: bool) -> None:
    """
    Fail closed when SECRET_KEY is unsafe for a production-like deployment.

    Production-like is signaled by ``debug=False`` (the existing dev/prod switch
    used across the codebase: ``settings.DEBUG``). In that case an unset/empty,
    the committed dev-default, or a too-short key raises InsecureSecretKeyError
    so the application refuses to boot. In DEBUG mode the dev-default is allowed
    but logged loudly.

    Args:
        secret_key: The configured SECRET_KEY value.
        debug: True when running in development/DEBUG mode.

    Raises:
        InsecureSecretKeyError: If the key is unsafe and debug is False.
    """
    is_unset = not secret_key
    is_dev_default = secret_key == DEV_DEFAULT_SECRET_KEY
    is_too_short = secret_key is not None and len(secret_key) < MIN_SECRET_KEY_LENGTH

    if not (is_unset or is_dev_default or is_too_short):
        return

    if debug:
        logger.warning(
            "SECRET_KEY is unset, the public development default, or shorter than "
            f"{MIN_SECRET_KEY_LENGTH} characters. This is INSECURE and only tolerated "
            "because DEBUG=True. Sessions can be forged by anyone. Set a strong, "
            "secret SECRET_KEY before running in production."
        )
        return

    if is_unset:
        reason = "SECRET_KEY is not set"
    elif is_dev_default:
        reason = "SECRET_KEY equals the publicly known development default"
    else:
        reason = f"SECRET_KEY is shorter than {MIN_SECRET_KEY_LENGTH} characters"

    raise InsecureSecretKeyError(
        f"Refusing to start: {reason} while DEBUG=False. The session cookie and "
        "websocket log-stream auth are signed with SECRET_KEY; a weak or public key "
        "lets an attacker forge a session for any user. Provide a strong, unique "
        "SECRET_KEY via the production secret (rig-system operations-manager env secret)."
    )
