"""
SECRET_KEY safety validation.

SECRET_KEY signs the session cookie (Starlette SessionMiddleware) and the
websocket log-stream session. A weak or publicly known key lets an attacker
forge a session for an arbitrary user, bypassing both HTTP and websocket auth.

Design: no hard-coded default in source. If SECRET_KEY is not set in env, the
default factory generates a fresh cryptographically random key per process.
Sessions then invalidate on restart -- acceptable for any deployment that has
not explicitly opted in to a stable key. Operators who want session stability
across reboots set SECRET_KEY in env (production already does this via the
SOPS-encrypted operations-manager env secret).

When SECRET_KEY is set, it must be at least MIN_SECRET_KEY_LENGTH characters
or startup fails closed. No cluster gate, no tolerated weak values.
"""

import logging
import secrets

logger = logging.getLogger(__name__)

# Minimum acceptable SECRET_KEY length. itsdangerous accepts any non-empty
# key, so we enforce a floor that makes brute-forcing infeasible.
MIN_SECRET_KEY_LENGTH = 32


class InsecureSecretKeyError(RuntimeError):
    """Raised at startup when SECRET_KEY is set but too short to be safe."""


def generate_secret_key() -> str:
    """
    Default factory: fresh per-process random key.

    Emits a single WARNING so operators see in logs that they are running
    without a persistent key (sessions die on restart, and a multi-replica
    deployment cannot share sessions across pods).
    """
    logger.warning(
        "SECRET_KEY not set; using a fresh random key for this process. "
        "Sessions will invalidate on restart and cannot be shared across "
        "replicas. Set SECRET_KEY in env for stable sessions."
    )
    return secrets.token_urlsafe(MIN_SECRET_KEY_LENGTH)


def validate_secret_key(secret_key: str) -> None:
    """
    Fail closed when SECRET_KEY is shorter than MIN_SECRET_KEY_LENGTH.

    The default factory produces a key well above this threshold, so this
    validator only fires when an operator explicitly set a too-short value.

    Raises:
        InsecureSecretKeyError: If the key is shorter than the minimum.
    """
    if len(secret_key) < MIN_SECRET_KEY_LENGTH:
        raise InsecureSecretKeyError(
            f"Refusing to start: SECRET_KEY must be at least {MIN_SECRET_KEY_LENGTH} "
            "characters. The session cookie and websocket log-stream auth are signed "
            "with SECRET_KEY; a weak key lets an attacker forge a session for any "
            "user. Either unset SECRET_KEY (a fresh random key is generated per "
            "process; sessions invalidate on restart) or provide a strong value via "
            "the cluster's operations-manager env secret."
        )
