"""Unit tests for the shared-TOTP helper (opi/utils/totp.py).

The critical invariant is the encoding contract: the raw secret stored in the
Keycloak credential and the Base32 secret handed to humans must reference the
same HMAC key bytes, so codes generated from the otpauth URI validate against
the stored credential.
"""

import base64
import json
from urllib.parse import parse_qs, urlsplit

from opi.utils.totp import (
    KEYCLOAK_ALGORITHM,
    OTPAUTH_ALGORITHM,
    TOTP_DIGITS,
    TOTP_PERIOD,
    build_credential_representation,
    build_otpauth_uri,
    generate_totp_secret,
    totp_base32,
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
