"""Bearer-token authentication for callers that act as a user.

The rest of this API authenticates per project: an X-API-Key that belongs to one
project and can do nothing outside it. That path cannot work for creating a
project, because the project -- and therefore its key -- does not exist yet.

This module adds the second, deliberately narrow way to recognise a caller: an
SSO access token, presented as ``Authorization: Bearer <token>`` (RFC 6750). The
API is a resource server here and nothing more. It issues no tokens, runs no
login flow and keeps no session; where the token came from is the caller's
problem. What it does do:

- verify the signature against the realm's JWKS, with a fixed set of asymmetric
  algorithms so a token cannot pick its own,
- verify issuer, audience and expiry against the realm's own discovery document,
- take the identity from the claims and then decide, separately, whether that
  identity may do this. A valid token says who someone is, not what they may do.

The existing session-based SSO for the web UI is untouched; this is a second way
to recognise a caller, not a second way to log in.
"""

import logging
import time
from collections.abc import Callable  # noqa: TC003 -- used in decorator signatures at runtime
from functools import wraps
from typing import Any

from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError
from fastapi import HTTPException
from opi.connectors.keycloak import fetch_jwks, fetch_oidc_metadata
from opi.core.config import settings
from opi.services.user_service import get_user_service
from starlette.requests import Request  # noqa: TC002 -- FastAPI needs Request at runtime

logger = logging.getLogger(__name__)

# Asymmetric signatures only, named explicitly. Without this list the token itself
# decides how it is verified, which is how "alg: none" and HMAC-with-the-public-key
# forgeries get in.
ALLOWED_ALGORITHMS = ["RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512"]

# Clock skew allowance for exp/nbf/iat, in seconds.
CLOCK_SKEW_SECONDS = 60

# How long a fetched discovery document and key set stay usable before they are
# fetched again.
METADATA_TTL_SECONDS = 3600

# A token whose key id is not in the cached JWKS may mean the realm rotated its
# keys, so the set is refetched. This is a network call an unauthenticated caller
# can trigger, so it happens at most once per this many seconds.
UNKNOWN_KID_REFRESH_INTERVAL_SECONDS = 60


class UserTokenError(Exception):
    """Raised when a bearer token cannot be accepted."""


class _MetadataCache:
    """Caches the realm's discovery document and key set.

    Both are fetched over the network and change rarely. The key set is
    additionally refetched when a token names a key id it does not contain, so a
    key rotation does not lock every caller out until the TTL expires.
    """

    def __init__(self) -> None:
        self._metadata: dict[str, Any] | None = None
        self._metadata_fetched_at: float = 0.0
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at: float = 0.0
        self._last_forced_refresh: float = 0.0

    def clear(self) -> None:
        """Drop everything cached. Used by tests and after a configuration change."""
        self._metadata = None
        self._metadata_fetched_at = 0.0
        self._jwks = None
        self._jwks_fetched_at = 0.0
        self._last_forced_refresh = 0.0

    async def get_metadata(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._metadata is not None and now - self._metadata_fetched_at < METADATA_TTL_SECONDS:
            return self._metadata

        discovery_url = settings.OIDC_DISCOVERY_URL
        if not discovery_url:
            raise UserTokenError("no OIDC discovery URL is configured")

        self._metadata = await fetch_oidc_metadata(discovery_url)
        self._metadata_fetched_at = now
        return self._metadata

    async def get_jwks(self, *, force: bool = False) -> dict[str, Any]:
        now = time.monotonic()
        if force:
            if now - self._last_forced_refresh < UNKNOWN_KID_REFRESH_INTERVAL_SECONDS:
                raise UserTokenError("unknown signing key")
            self._last_forced_refresh = now
        elif self._jwks is not None and now - self._jwks_fetched_at < METADATA_TTL_SECONDS:
            return self._jwks

        metadata = await self.get_metadata()
        jwks_uri = metadata.get("jwks_uri")
        if not jwks_uri:
            raise UserTokenError("the OIDC discovery document has no jwks_uri")

        self._jwks = await fetch_jwks(str(jwks_uri))
        self._jwks_fetched_at = now
        return self._jwks


_metadata_cache = _MetadataCache()


def get_metadata_cache() -> _MetadataCache:
    """Return the process-wide metadata cache (tests clear it between cases)."""
    return _metadata_cache


def extract_bearer_token(request: Request) -> str:
    """Return the bearer token from the Authorization header.

    Raises:
        UserTokenError: When the header is absent or not a bearer credential.
    """
    header = request.headers.get("Authorization")
    if not header:
        raise UserTokenError("no Authorization header")

    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UserTokenError("the Authorization header is not a Bearer credential")

    return token.strip()


async def verify_user_token(token: str) -> dict[str, Any]:
    """Verify a bearer token and return its claims.

    Checks the signature against the realm's published keys and then the issuer,
    audience and expiry. Nothing in the token is believed before its signature is.

    Args:
        token: The raw JWT from the Authorization header

    Returns:
        The verified claims

    Raises:
        UserTokenError: When the token cannot be verified.
    """
    metadata = await _metadata_cache.get_metadata()
    issuer = metadata.get("issuer")
    if not issuer:
        raise UserTokenError("the OIDC discovery document has no issuer")

    claims_options = {
        "iss": {"essential": True, "value": str(issuer)},
        "aud": {"essential": True, "value": settings.CLI_TOKEN_AUDIENCE},
        "exp": {"essential": True},
    }

    jwt = JsonWebToken(ALLOWED_ALGORITHMS)
    jwks = await _metadata_cache.get_jwks()

    try:
        claims = jwt.decode(token, JsonWebKey.import_key_set(jwks), claims_options=claims_options)
    except ValueError:
        # authlib raises ValueError when the key set has no key for this token's
        # kid. That is what a key rotation looks like, so refetch once and retry.
        jwks = await _metadata_cache.get_jwks(force=True)
        try:
            claims = jwt.decode(token, JsonWebKey.import_key_set(jwks), claims_options=claims_options)
        except (JoseError, ValueError) as exc:
            raise UserTokenError(f"token verification failed: {exc}") from exc
    except JoseError as exc:
        raise UserTokenError(f"token verification failed: {exc}") from exc

    try:
        claims.validate(leeway=CLOCK_SKEW_SECONDS)
    except JoseError as exc:
        raise UserTokenError(f"token claims rejected: {exc}") from exc

    return dict(claims)


def authorize_claims(claims: dict[str, Any]) -> dict[str, Any]:
    """Decide whether the identity in these claims may use this API.

    A verified token establishes who the caller is; whether that person may act
    is this platform's decision and is made here, against the same allowlist the
    web UI uses. An unverified email is refused outright, because every check
    below keys on the email address.

    Args:
        claims: The verified token claims

    Returns:
        The caller's identity, in the shape the rest of the app uses for a user

    Raises:
        UserTokenError: When the identity may not use this API.
    """
    email = claims.get("email")
    if not email or not isinstance(email, str):
        raise UserTokenError("the token carries no email claim")

    if claims.get("email_verified") is not True:
        raise UserTokenError("the email in this token is not verified")

    if not get_user_service().is_email_allowed(email):
        raise UserTokenError("this user has no access to the platform")

    return {
        "sub": claims.get("sub"),
        "email": email,
        "name": claims.get("name", claims.get("preferred_username", "Unknown")),
        "preferred_username": claims.get("preferred_username"),
    }


def validate_user_token(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator requiring a valid SSO bearer token on a route.

    On success the caller lands in ``request.state.user`` in the same shape the
    session-based path uses, so everything downstream (task ownership, logging)
    sees one kind of user.
    """

    @wraps(func)
    async def wrapper(*args: Any, request: Request, **kwargs: Any) -> Any:
        try:
            token = extract_bearer_token(request)
            claims = await verify_user_token(token)
            user = authorize_claims(claims)
        except UserTokenError as exc:
            logger.warning("Bearer authentication failed for route %s: %s", func.__name__, exc)
            raise HTTPException(
                status_code=401,
                detail="Authentication required - provide a valid Authorization: Bearer token",
                headers={"WWW-Authenticate": 'Bearer realm="zad"'},
            ) from exc

        logger.debug("Bearer authentication successful for route %s (user: %s)", func.__name__, user["email"])
        request.state.user = user
        return await func(*args, request=request, **kwargs)

    return wrapper
