"""Per-deployment wake token: mint, AGE-encrypt for storage, read back, compare.

A wake token is scoped to one deployment. It is deliberately NOT the project API
key (which can delete deployments and change images): a token leaking from a
publicly reachable waker pod may do nothing but wake its own deployment. The token
is minted, AGE-encrypted with the project key and stored on
``deployments[].sleep.wake-token`` so OPI can read it back, and separately rendered
as a SOPS Secret into the namespace for the waker pod's envFrom.
"""

from __future__ import annotations

import secrets
from typing import Any

from opi.utils.age import decrypt_age_content, encrypt_age_content_sync, get_decoded_project_private_key
from opi.utils.api_keys import generate_api_key

#: The env-var the waker reads its token from (matches the Secret key in secret.py).
WAKE_TOKEN_ENV = "ZAD_WAKE_TOKEN"


def mint() -> str:
    """Mint a fresh random wake token."""
    return generate_api_key()


def encrypt(plain_token: str, project_data: dict[str, Any]) -> str:
    """AGE-encrypt a token with the project's public key for storage in the file."""
    public_key = project_data.get("config", {}).get("age-public-key")
    if not public_key:
        raise ValueError("Project public key required to encrypt the wake token")
    return encrypt_age_content_sync(plain_token, public_key)


async def decrypt(encrypted_token: str, project_data: dict[str, Any]) -> str:
    """Decrypt a stored wake token back to plaintext using the project's private key."""
    private_key = await get_decoded_project_private_key(project_data)
    return await decrypt_age_content(encrypted_token, private_key)


def compare(candidate: str, expected: str) -> bool:
    """Constant-time compare of a presented token against the expected one."""
    if not candidate or not expected:
        return False
    return secrets.compare_digest(candidate, expected)
