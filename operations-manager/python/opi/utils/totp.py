"""TOTP (OTP) secrets: genereren, tonen en verifieren.

Twee gebruikers van deze module, met verschillende doelen. De OTP op de ZAD-gebruiker
(``opi/services/user_otp_service.py``, ``features/otp-op-de-zad-gebruiker.md``) hangt aan
een persoon en wordt door OPI zelf geverifieerd (``verify_totp``). Daaronder staat de
oudere, gedeelde variant voor Keycloak-realm-admins, die Keycloak zelf verifieert; de
encoding-afspraak hieronder komt daarvandaan en geldt voor beide.

Gedeelde TOTP-geheimen voor automatisch aangemaakte Keycloak-realm-admins:

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

# Tolerantie bij het verifieren: een stap terug en een vooruit, voor klokverschil
# tussen de server en het toestel van de gebruiker.
TOTP_TOLERANCE_STEPS = 1


def generate_totp_secret() -> str:
    """Generate a random raw TOTP secret string (the value Keycloak stores)."""
    return "".join(secrets.choice(_SECRET_ALPHABET) for _ in range(_SECRET_LENGTH))


def totp_base32(secret: str) -> str:
    """Return the Base32 form of a raw secret, for the otpauth URI / manual entry."""
    return base64.b32encode(secret.encode()).decode("ascii").rstrip("=")


def _code_for_step(secret: str, step: int) -> str:
    """The TOTP code for one time step, from the raw secret's bytes as HMAC key."""
    digest = hmac.new(secret.encode(), struct.pack(">Q", step), hashlib.sha1).digest()
    # RFC 6238 dynamic truncation: the low nibble of the last byte picks the offset.
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % 10**TOTP_DIGITS).zfill(TOTP_DIGITS)


def totp_step(at: float | None = None) -> int:
    """The time step a moment falls in."""
    return int((time.time() if at is None else at) // TOTP_PERIOD)


def totp_now(secret: str, at: float | None = None) -> tuple[str, int]:
    """Return the current TOTP code and the seconds it stays valid.

    The portal shows this code instead of the seed: a code expires within one
    period, the seed would grant codes forever. Uses the raw secret's bytes as
    the HMAC key, matching what Keycloak stores in ``secretData.value``.
    """
    now = time.time() if at is None else at
    seconds_remaining = TOTP_PERIOD - int(now) % TOTP_PERIOD
    return _code_for_step(secret, totp_step(now)), seconds_remaining


def verify_totp(
    secret: str,
    code: str,
    now: float | None = None,
    last_used_step: int | None = None,
) -> int | None:
    """The accepted time step for ``code``, or ``None`` when it is not valid.

    A step up to and including ``last_used_step`` is refused: without that, an
    intercepted code stays usable for the rest of its period.
    """
    entered = code.strip()
    if not secret or len(entered) != TOTP_DIGITS or not entered.isdigit():
        return None

    current = totp_step(now)
    accepted: int | None = None
    # Elke kandidaat wordt nagerekend, ook na een treffer: een lus die eruit springt
    # verraadt via zijn looptijd welke stap het was.
    for step in range(current - TOTP_TOLERANCE_STEPS, current + TOTP_TOLERANCE_STEPS + 1):
        if last_used_step is not None and step <= last_used_step:
            continue
        if hmac.compare_digest(_code_for_step(secret, step), entered):
            accepted = step
    return accepted


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
