"""Unit tests for the shared-TOTP helper (opi/utils/totp.py).

The critical invariant is the encoding contract: the raw secret stored in the
Keycloak credential and the Base32 secret handed to humans must reference the
same HMAC key bytes, so codes generated from the otpauth URI validate against
the stored credential.
"""

import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from opi.utils.totp import (
    KEYCLOAK_ALGORITHM,
    OTPAUTH_ALGORITHM,
    TOTP_DIGITS,
    TOTP_PERIOD,
    build_credential_representation,
    build_otpauth_uri,
    generate_totp_secret,
    totp_base32,
    totp_now,
)


def test_generate_secret_is_printable_and_non_deterministic() -> None:
    a = generate_totp_secret()
    b = generate_totp_secret()
    assert a != b
    assert a.isascii()
    assert a.isalnum()
    assert len(a) >= 20


def test_base32_round_trips_to_raw_secret_bytes() -> None:
    """The otpauth Base32 must decode back to the raw secret's bytes.

    This is the HMAC-key contract: Keycloak uses the raw secret string's bytes
    as the key; the authenticator decodes the Base32 to the same bytes.
    """
    secret = generate_totp_secret()
    b32 = totp_base32(secret)
    # Re-pad for the stdlib decoder (build strips '=' padding for display).
    padded = b32 + "=" * (-len(b32) % 8)
    assert base64.b32decode(padded) == secret.encode()


def test_base32_has_no_padding() -> None:
    assert "=" not in totp_base32(generate_totp_secret())


def test_credential_representation_shape() -> None:
    rep = build_credential_representation("RAWSECRET", label="My OTP")
    assert rep["type"] == "otp"
    assert rep["userLabel"] == "My OTP"
    assert json.loads(rep["secretData"]) == {"value": "RAWSECRET"}
    cred_data = json.loads(rep["credentialData"])
    assert cred_data["subType"] == "totp"
    assert cred_data["digits"] == TOTP_DIGITS == 6
    assert cred_data["period"] == TOTP_PERIOD == 30
    assert cred_data["algorithm"] == KEYCLOAK_ALGORITHM == "HmacSHA1"


def test_credential_representation_default_label() -> None:
    assert build_credential_representation("x")["userLabel"] == "ZAD shared OTP"


def test_otpauth_uri_structure_and_params() -> None:
    secret = generate_totp_secret()
    uri = build_otpauth_uri(secret, account_name="proj_local_admin", issuer="proj-local")
    split = urlsplit(uri)
    assert split.scheme == "otpauth"
    assert split.netloc == "totp"
    assert split.path == "/proj-local:proj_local_admin"
    params = parse_qs(split.query)
    assert params["secret"][0] == totp_base32(secret)
    assert params["issuer"][0] == "proj-local"
    assert params["algorithm"][0] == OTPAUTH_ALGORITHM == "SHA1"
    assert params["digits"][0] == str(TOTP_DIGITS)
    assert params["period"][0] == str(TOTP_PERIOD)


def test_otpauth_uri_url_encodes_label() -> None:
    uri = build_otpauth_uri("s", account_name="a b", issuer="RIG Project")
    # Spaces in label/issuer must be percent-encoded, not raw.
    assert " " not in uri
    assert "RIG%20Project" in uri


# RFC 6238 Appendix B, SHA-1 vectors. The RFC lists 8-digit codes; ours are the
# low 6 digits of those, since the truncation only differs in the final modulo.
@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
        (20000000000, "353130"),
    ],
)
def test_totp_now_matches_rfc6238_vectors(at: int, expected: str) -> None:
    # The RFC's SHA-1 seed is this ASCII string, used verbatim as the HMAC key -
    # exactly how we treat the raw secret Keycloak stores.
    assert totp_now("12345678901234567890", at=at)[0] == expected


def test_totp_now_agrees_with_the_secret_handed_to_authenticators() -> None:
    """A code from the raw secret must match one derived from the otpauth Base32.

    This closes the loop the encoding contract promises: what an authenticator
    computes from the URI is what the portal shows.
    """
    secret = generate_totp_secret()
    b32 = totp_base32(secret)
    padded = b32 + "=" * (-len(b32) % 8)
    from_uri = base64.b32decode(padded).decode()
    assert totp_now(from_uri, at=1700000000) == totp_now(secret, at=1700000000)


def test_totp_now_code_is_six_digits_and_zero_padded() -> None:
    # 1546962600 lands on a code starting with a zero; str() alone would drop it.
    code, _ = totp_now("12345678901234567890", at=1234567890)
    assert code == "005924"
    assert len(code) == TOTP_DIGITS


def test_totp_now_seconds_remaining_counts_down_within_the_period() -> None:
    assert totp_now("s", at=1000.0)[1] == TOTP_PERIOD - (1000 % TOTP_PERIOD)
    # Start of a period: a full period of validity, never zero.
    assert totp_now("s", at=1200.0)[1] == TOTP_PERIOD
    # Last second of that period.
    assert totp_now("s", at=1229.9)[1] == 1


def test_totp_now_code_is_stable_within_a_period_and_changes_after() -> None:
    secret = generate_totp_secret()
    assert totp_now(secret, at=1200.0)[0] == totp_now(secret, at=1229.9)[0]
    assert totp_now(secret, at=1200.0)[0] != totp_now(secret, at=1230.0)[0]
