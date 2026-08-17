"""Tests for the SSO bearer-token authentication path.

This is the second way into the API, next to the per-project API key, and it is
the only one that can work before a project exists. Everything here is about
refusing tokens: a token that verifies is one case, and the ways a token can be
wrong are the rest.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from authlib.jose import JsonWebKey, JsonWebToken, jwt
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException
from opi.api.user_token_auth import (
    UserTokenError,
    authorize_claims,
    extract_bearer_token,
    get_metadata_cache,
    validate_user_token,
    verify_user_token,
)
from starlette.datastructures import Headers

ISSUER = "https://keycloak.example.test/realms/operations-manager"
AUDIENCE = "zad-api"


@pytest.fixture
def signing_key() -> Any:
    """A private RSA key that stands in for the realm's signing key."""
    return JsonWebKey.generate_key("RSA", 2048, is_private=True)


@pytest.fixture
def other_key() -> Any:
    """A second private key, standing in for a signer we do not trust."""
    return JsonWebKey.generate_key("RSA", 2048, is_private=True)


def _jwks_of(key: Any) -> dict[str, Any]:
    """The public key set a realm would publish for this key."""
    public = key.as_dict(is_private=False)
    public["kid"] = key.thumbprint()
    public["alg"] = "RS256"
    return {"keys": [public]}


def _make_token(
    key: Any,
    *,
    issuer: str = ISSUER,
    audience: Any = AUDIENCE,
    expires_in: int = 300,
    algorithm: str = "RS256",
    email: str = "user@example.test",
    email_verified: bool = True,
) -> str:
    now = int(time.time())
    header = {"alg": algorithm, "kid": key.thumbprint()}
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": "user-1",
        "iat": now,
        "exp": now + expires_in,
        "email": email,
        "email_verified": email_verified,
        "preferred_username": "user",
    }
    return jwt.encode(header, payload, key).decode()


@pytest.fixture
def realm(signing_key: Any) -> Any:
    """Patch the network calls so the realm serves our test key set."""
    get_metadata_cache().clear()
    metadata = {"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"}
    with (
        patch("opi.api.user_token_auth.fetch_oidc_metadata", AsyncMock(return_value=metadata)) as meta,
        patch("opi.api.user_token_auth.fetch_jwks", AsyncMock(return_value=_jwks_of(signing_key))) as jwks,
        patch("opi.api.user_token_auth.settings") as mock_settings,
    ):
        mock_settings.OIDC_DISCOVERY_URL = f"{ISSUER}/.well-known/openid-configuration"
        mock_settings.CLI_TOKEN_AUDIENCE = AUDIENCE
        yield {"metadata": meta, "jwks": jwks, "settings": mock_settings}
    get_metadata_cache().clear()


class _FakeRequest:
    """Minimal stand-in for a Starlette request: headers plus a state object."""

    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = Headers(headers)
        self.state = type("State", (), {})()


class TestExtractBearerToken:
    """The header itself, before anything is verified."""

    def test_returns_token(self) -> None:
        assert extract_bearer_token(_FakeRequest({"Authorization": "Bearer abc.def.ghi"})) == "abc.def.ghi"

    def test_scheme_is_case_insensitive(self) -> None:
        assert extract_bearer_token(_FakeRequest({"Authorization": "bearer abc"})) == "abc"

    def test_missing_header_is_refused(self) -> None:
        with pytest.raises(UserTokenError):
            extract_bearer_token(_FakeRequest({}))

    def test_other_scheme_is_refused(self) -> None:
        """An API key in the Authorization header is not a bearer token."""
        with pytest.raises(UserTokenError):
            extract_bearer_token(_FakeRequest({"Authorization": "Basic dXNlcjpwYXNz"}))

    def test_empty_credential_is_refused(self) -> None:
        with pytest.raises(UserTokenError):
            extract_bearer_token(_FakeRequest({"Authorization": "Bearer   "}))


@pytest.mark.asyncio
class TestVerifyUserToken:
    """Signature, issuer, audience and expiry."""

    async def test_valid_token_returns_claims(self, realm: Any, signing_key: Any) -> None:
        claims = await verify_user_token(_make_token(signing_key))
        assert claims["email"] == "user@example.test"
        assert claims["iss"] == ISSUER

    async def test_token_signed_by_another_key_is_refused(self, realm: Any, other_key: Any) -> None:
        """A well-formed token from a signer the realm does not publish."""
        with pytest.raises(UserTokenError):
            await verify_user_token(_make_token(other_key))

    async def test_wrong_issuer_is_refused(self, realm: Any, signing_key: Any) -> None:
        with pytest.raises(UserTokenError):
            await verify_user_token(_make_token(signing_key, issuer="https://evil.example.test/realms/other"))

    async def test_wrong_audience_is_refused(self, realm: Any, signing_key: Any) -> None:
        """A token minted for another client must not open this API."""
        with pytest.raises(UserTokenError):
            await verify_user_token(_make_token(signing_key, audience="some-other-client"))

    async def test_audience_list_containing_ours_is_accepted(self, realm: Any, signing_key: Any) -> None:
        claims = await verify_user_token(_make_token(signing_key, audience=["account", AUDIENCE]))
        assert AUDIENCE in claims["aud"]

    async def test_expired_token_is_refused(self, realm: Any, signing_key: Any) -> None:
        with pytest.raises(UserTokenError):
            await verify_user_token(_make_token(signing_key, expires_in=-3600))

    async def test_unsigned_token_is_refused(self, realm: Any, signing_key: Any) -> None:
        """alg: none must never be accepted, however well-formed the claims are."""
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": AUDIENCE, "exp": now + 300, "email": "user@example.test"}
        unsigned = JsonWebToken(["none"]).encode({"alg": "none"}, payload, "").decode()
        with pytest.raises(UserTokenError):
            await verify_user_token(unsigned)

    async def test_symmetric_signature_with_public_key_is_refused(self, realm: Any, signing_key: Any) -> None:
        """The classic HS256-signed-with-the-RSA-public-key forgery."""
        public_pem = signing_key.get_public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        now = int(time.time())
        payload = {"iss": ISSUER, "aud": AUDIENCE, "exp": now + 300, "email": "user@example.test"}

        # Hand-rolled: authlib refuses to sign this, an attacker does not.
        def b64(raw: bytes) -> bytes:
            return base64.urlsafe_b64encode(raw).rstrip(b"=")

        header = b64(json.dumps({"alg": "HS256", "kid": signing_key.thumbprint()}).encode())
        body = b64(json.dumps(payload).encode())
        signing_input = header + b"." + body
        signature = b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
        forged = (signing_input + b"." + signature).decode()

        with pytest.raises(UserTokenError):
            await verify_user_token(forged)

    async def test_garbage_is_refused(self, realm: Any) -> None:
        with pytest.raises(UserTokenError):
            await verify_user_token("not-a-token")

    async def test_no_discovery_url_configured_is_refused(self, realm: Any, signing_key: Any) -> None:
        """Without a configured realm nothing can be verified, so nothing is accepted."""
        get_metadata_cache().clear()
        realm["settings"].OIDC_DISCOVERY_URL = None
        with pytest.raises(UserTokenError):
            await verify_user_token(_make_token(signing_key))

    async def test_key_set_is_cached_between_calls(self, realm: Any, signing_key: Any) -> None:
        await verify_user_token(_make_token(signing_key))
        await verify_user_token(_make_token(signing_key))
        assert realm["jwks"].await_count == 1


class TestAuthorizeClaims:
    """A verified token says who someone is; this decides what that is worth."""

    def _claims(self, **overrides: Any) -> dict[str, Any]:
        claims = {
            "sub": "user-1",
            "email": "user@example.test",
            "email_verified": True,
            "preferred_username": "user",
            "name": "Test User",
        }
        claims.update(overrides)
        return claims

    def test_allowed_user_passes(self) -> None:
        with patch("opi.api.user_token_auth.get_user_service") as get_service:
            get_service.return_value.is_email_allowed.return_value = True
            user = authorize_claims(self._claims())
        assert user["email"] == "user@example.test"
        assert user["name"] == "Test User"

    def test_user_outside_the_allowlist_is_refused(self) -> None:
        """A valid token is not permission; the platform allowlist still decides."""
        with patch("opi.api.user_token_auth.get_user_service") as get_service:
            get_service.return_value.is_email_allowed.return_value = False
            with pytest.raises(UserTokenError):
                authorize_claims(self._claims())

    def test_unverified_email_is_refused(self) -> None:
        """Every check keys on the email, so an unverified one may not establish access."""
        with patch("opi.api.user_token_auth.get_user_service") as get_service:
            get_service.return_value.is_email_allowed.return_value = True
            with pytest.raises(UserTokenError):
                authorize_claims(self._claims(email_verified=False))

    def test_missing_email_is_refused(self) -> None:
        with patch("opi.api.user_token_auth.get_user_service") as get_service:
            get_service.return_value.is_email_allowed.return_value = True
            with pytest.raises(UserTokenError):
                authorize_claims(self._claims(email=None))


@pytest.mark.asyncio
class TestValidateUserTokenDecorator:
    """What a route sees."""

    async def test_valid_token_reaches_the_route_and_sets_the_user(self, realm: Any, signing_key: Any) -> None:
        seen: dict[str, Any] = {}

        @validate_user_token
        async def route(request: Any) -> str:
            seen["user"] = request.state.user
            return "ok"

        request = _FakeRequest({"Authorization": f"Bearer {_make_token(signing_key)}"})
        with patch("opi.api.user_token_auth.get_user_service") as get_service:
            get_service.return_value.is_email_allowed.return_value = True
            assert await route(request=request) == "ok"

        assert seen["user"]["email"] == "user@example.test"

    async def test_missing_token_gives_401_with_a_challenge(self, realm: Any) -> None:
        @validate_user_token
        async def route(request: Any) -> str:  # pragma: no cover - must not be reached
            return "ok"

        with pytest.raises(HTTPException) as exc:
            await route(request=_FakeRequest({}))

        assert exc.value.status_code == 401
        assert exc.value.headers is not None
        assert exc.value.headers["WWW-Authenticate"].startswith("Bearer")

    async def test_refused_user_never_reaches_the_route(self, realm: Any, signing_key: Any) -> None:
        called = False

        @validate_user_token
        async def route(request: Any) -> str:
            nonlocal called
            called = True
            return "ok"

        request = _FakeRequest({"Authorization": f"Bearer {_make_token(signing_key)}"})
        with patch("opi.api.user_token_auth.get_user_service") as get_service:
            get_service.return_value.is_email_allowed.return_value = False
            with pytest.raises(HTTPException) as exc:
                await route(request=request)

        assert exc.value.status_code == 401
        assert called is False
