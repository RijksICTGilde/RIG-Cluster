"""
SECRET_KEY safety validation.

SECRET_KEY signs the session cookie (Starlette SessionMiddleware) and the
websocket log-stream session. A weak or publicly known key lets an attacker
forge a session for an arbitrary user, bypassing both HTTP and websocket auth.

This module holds the pure validation logic with no pydantic dependency so it
can be unit-tested in isolation and reused by the Settings model validator.

Why the gate is CLUSTER_MANAGER, not DEBUG: ``DEBUG`` is set inconsistently and
cannot signal production. The production deployment never sets the boolean
``DEBUG`` env var (it only sets the unrelated string ``DEBUG_MODE`` that selects
the uvicorn run mode), so ``DEBUG`` defaults to True in production. Meanwhile the
committed local dev ``.env`` sets ``DEBUG=false``. A DEBUG gate would therefore
both miss production and break local development. ``CLUSTER_MANAGER`` is set
consistently per environment (``local`` / ``sandboxed-local`` for dev,
``odcn-production`` for prod) and is the reliable discriminator. The gate is
fail-closed: anything that is not a known local/sandbox cluster manager must
have a strong SECRET_KEY or it refuses to boot.
"""

import logging

logger = logging.getLogger(__name__)

# Well-known development default for SECRET_KEY. This value is committed to the
# public repository, so anyone can forge a signed session cookie when it is used.
# It is only acceptable on a known local/sandbox cluster.
DEV_DEFAULT_SECRET_KEY = "default-secret-key-for-development-change-in-production"

# Minimum acceptable SECRET_KEY length for production. itsdangerous accepts any
# non-empty key, so we enforce a floor that makes brute-forcing infeasible.
MIN_SECRET_KEY_LENGTH = 32

# CLUSTER_MANAGER values that designate a local developer / sandbox cluster.
# Anything else (notably "odcn-production", or an unset/typo'd value) is treated
# as production-like and fails closed on a weak SECRET_KEY.
LOCAL_CLUSTER_MANAGERS = frozenset({"local", "sandboxed-local"})


class InsecureSecretKeyError(RuntimeError):
    """Raised at startup when SECRET_KEY is unsafe for a production-like deployment."""


def is_local_cluster(cluster_manager: str | None) -> bool:
    """
    Return True only for a known local/sandbox cluster manager.

    Unknown, empty, or production values return False so they fail closed.
    """
    return (cluster_manager or "").strip().lower() in LOCAL_CLUSTER_MANAGERS


def validate_secret_key(secret_key: str | None, cluster_manager: str | None) -> None:
    """
    Fail closed when SECRET_KEY is unsafe for a production-like deployment.

    A weak key (unset/empty, the committed dev-default, or shorter than
    MIN_SECRET_KEY_LENGTH) is tolerated with a loud warning only on a known
    local/sandbox cluster. On any other cluster manager (production, or an
    unknown/misconfigured value) it raises InsecureSecretKeyError so the
    application refuses to boot.

    Args:
        secret_key: The configured SECRET_KEY value.
        cluster_manager: The CLUSTER_MANAGER value (settings.CLUSTER_MANAGER).

    Raises:
        InsecureSecretKeyError: If the key is unsafe and the cluster manager is
            not a known local/sandbox cluster.
    """
    is_unset = not secret_key
    is_dev_default = secret_key == DEV_DEFAULT_SECRET_KEY
    is_too_short = secret_key is not None and len(secret_key) < MIN_SECRET_KEY_LENGTH

    if not (is_unset or is_dev_default or is_too_short):
        return

    if is_local_cluster(cluster_manager):
        logger.warning(
            "SECRET_KEY is unset, the public development default, or shorter than "
            f"{MIN_SECRET_KEY_LENGTH} characters. This is INSECURE and only tolerated "
            f"because CLUSTER_MANAGER={cluster_manager!r} is a local/sandbox cluster. "
            "Sessions can be forged by anyone. Set a strong, secret SECRET_KEY before "
            "running in production."
        )
        return

    if is_unset:
        reason = "SECRET_KEY is not set"
    elif is_dev_default:
        reason = "SECRET_KEY equals the publicly known development default"
    else:
        reason = f"SECRET_KEY is shorter than {MIN_SECRET_KEY_LENGTH} characters"

    raise InsecureSecretKeyError(
        f"Refusing to start: {reason} while CLUSTER_MANAGER={cluster_manager!r} is "
        "not a known local/sandbox cluster. The session cookie and websocket "
        "log-stream auth are signed with SECRET_KEY; a weak or public key lets an "
        "attacker forge a session for any user. Provide a strong, unique SECRET_KEY "
        "via the production secret (rig-system operations-manager env secret)."
    )
