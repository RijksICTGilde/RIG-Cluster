"""
SECRET_KEY safety validation.

SECRET_KEY signs the session cookie (Starlette SessionMiddleware) and the
websocket log-stream session. A weak or publicly known key lets an attacker
forge a session for an arbitrary user, bypassing both HTTP and websocket auth.

There is no hard-coded default in source: without SECRET_KEY in the environment the
default factory generates a fresh cryptographically random key per process, and sessions
then invalidate on restart. Session stability across reboots asks for SECRET_KEY in the
environment (production sets it through the SOPS-encrypted operations-manager env secret).

When SECRET_KEY is set, it must be at least MIN_SECRET_KEY_LENGTH characters
or startup fails closed. No cluster gate, no tolerated weak values.
"""

import logging
import secrets

logger = logging.getLogger(__name__)

# itsdangerous accepts any non-empty key, so this is the floor that makes brute-forcing
# infeasible.
MIN_SECRET_KEY_LENGTH = 32


class InsecureSecretKeyError(RuntimeError):
    """Raised at startup when SECRET_KEY is set but too short to be safe."""


def generate_secret_key() -> str:
    """Emits a single WARNING, so operators see in the logs that they run without a persistent
    key: sessions die on restart, and a multi-replica deployment cannot share them across pods.
    """
    logger.warning(
        "SECRET_KEY not set; using a fresh random key for this process. "
        "Sessions will invalidate on restart and cannot be shared across "
        "replicas. Set SECRET_KEY in env for stable sessions."
    )
    return secrets.token_urlsafe(MIN_SECRET_KEY_LENGTH)


def validate_secret_key(secret_key: str) -> None:
    """Raises ``InsecureSecretKeyError`` below ``MIN_SECRET_KEY_LENGTH``.

    The default factory produces a key well above that threshold, so this only fires when an
    operator explicitly set a too-short value.
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
