"""Shared TOTP (OTP) secrets for auto-provisioned Keycloak realm-admin accounts.

The realm-admin accounts are shared service accounts: OPI generates a single
TOTP seed, stores it (AGE-encrypted) in the project file alongside the admin
password, and surfaces it to all project admins. Each admin loads the same seed
into their password manager or authenticator app, so they all produce identical
codes and shared realm access keeps working.

Encoding contract (matches Keycloak's own TOTP handling):
- The raw secret string is stored verbatim in the Keycloak credential's
  ``secretData.value`` and used directly as the HMAC key (its UTF-8 bytes).
- The ``secret`` parameter of the ``otpauth://`` URI - what humans type/scan -
  is the Base32 encoding of those same bytes.
"""

import base64
import hashlib
import hmac
import json
import secrets
import string
import struct
import time
from urllib.parse import quote

# Keycloak credential parameters. Kept in one place so the stored credential and
# the otpauth URI can never drift apart.
TOTP_DIGITS = 6
TOTP_PERIOD = 30
KEYCLOAK_ALGORITHM = "HmacSHA1"  # value Keycloak stores in credentialData
OTPAUTH_ALGORITHM = "SHA1"  # value authenticator apps expect in the URI

# Length of the raw secret string. 32 alphanumeric chars is ample entropy and
# stays printable ASCII so it round-trips cleanly through JSON and Base32.
_SECRET_LENGTH = 32
_SECRET_ALPHABET = string.ascii_letters + string.digits


def generate_totp_secret() -> str:
    """Generate a random raw TOTP secret string (the value Keycloak stores)."""
    return "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(_SECRET_LENGTH))


def totp_base32(secret: str) -> str:
    """Return the Base32 form of a raw secret, for the otpauth URI / manual entry."""
    return base64.b32encode(secret.encode()).decode("ascii").rstrip("=")


def totp_now(secret: str, at: float | None = None) -> tuple[str, int]:
    """Return the current TOTP code and the seconds it stays valid.

    The portal shows this code instead of the seed: a code expires within one
    period, the seed would grant codes forever. Uses the raw secret's bytes as
    the HMAC key, matching what Keycloak stores in ``secretData.value``.
    """
    now = time.time() if at is None else at
    counter = int(now // TOTP_PERIOD)

    digest = hmac.new(secret.encode(), struct.pack(">Q", counter), hashlib.sha1).digest()
    # RFC 6238 dynamic truncation: the low nibble of the last byte picks the offset.
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF

    seconds_remaining = TOTP_PERIOD - int(now) % TOTP_PERIOD
    return str(code % 10**TOTP_DIGITS).zfill(TOTP_DIGITS), seconds_remaining


def build_credential_representation(secret: str, label: str = "ZAD shared OTP") -> dict[str, str]:
    """Build a Keycloak OTP CredentialRepresentation for a known raw secret.

    This is the realm export/import form (``secretData`` + ``credentialData``),
    which Keycloak imports verbatim when a user is created with this credential
    in its ``credentials`` array.
    """
    return {
        "type": "otp",
        "userLabel": label,
        "secretData": json.dumps({"value": secret}),
        "credentialData": json.dumps(
            {
                "subType": "totp",
                "digits": TOTP_DIGITS,
                "counter": 0,
                "period": TOTP_PERIOD,
                "algorithm": KEYCLOAK_ALGORITHM,
            }
        ),
    }


def build_otpauth_uri(secret: str, account_name: str, issuer: str) -> str:
    """Build an ``otpauth://totp/...`` provisioning URI from a raw secret.

    Password managers (1Password, Bitwarden) and authenticator apps ingest this
    URI directly; the embedded ``secret`` is the Base32 of the raw secret.
    """
    # The colon separating issuer and account in the label is a literal
    # separator in the otpauth spec, so encode the two parts independently.
    label = f"{quote(issuer)}:{quote(account_name)}"
    issuer_q = quote(issuer)
    return (
        f"otpauth://totp/{label}"
        f"?secret={totp_base32(secret)}"
        f"&issuer={issuer_q}"
        f"&algorithm={OTPAUTH_ALGORITHM}"
        f"&digits={TOTP_DIGITS}"
        f"&period={TOTP_PERIOD}"
    )
