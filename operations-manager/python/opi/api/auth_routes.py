"""The login, logout and OAuth callback endpoints of the Keycloak SSO flow."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import Response

from opi.http.session import ServerSideSession
from opi.services.session_revocation_service import revoke_sessions
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from authlib.integrations.starlette_client import StarletteOAuth2App

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/auth", tags=["authentication"])


@auth_router.get("/login")
async def login(request: Request) -> Response:
    """Start the OAuth flow by redirecting to Keycloak's authorization endpoint."""
    try:
        oauth = request.app.state.oauth

        if not hasattr(oauth, "keycloak"):
            raise HTTPException(
                status_code=500, detail="Authentication system not initialized - startup may have failed"
            )

        keycloak = oauth.keycloak

        redirect_uri = str(request.url_for("auth_callback"))
        logger.info(f"Initiating OAuth login with redirect URI: {redirect_uri}")

        return await keycloak.authorize_redirect(request, redirect_uri)

    except Exception as e:
        logger.exception("Error initiating OAuth login")

        if "Name or service not known" in str(e):
            logger.error(
                "DNS resolution failed during OAuth login - this indicates the discovery URL cannot be resolved"
            )
            from opi.core.config import settings

            logger.error(f"OIDC_DISCOVERY_URL being used: {settings.OIDC_DISCOVERY_URL}")
            logger.error("Check if this URL is accessible from inside the Kubernetes pod")
            logger.error(
                "For production, it should typically be an external URL like: https://keycloak.rijksapp.nl/realms/rig-platform/.well-known/openid-configuration"
            )

        # The cause is in the log above, under the mark of this request. What the user gets
        # names that same mark and gives nothing away about where the login service lives.
        raise HTTPException(
            status_code=500, detail="Inloggen is niet gelukt. Probeer het over een minuut opnieuw."
        ) from e


@auth_router.get("/login/beheer")
async def login_beheer(request: Request) -> Response:
    """The break-glass login: straight into the platform realm, past SSO Rijk.

    A local administrator account with a password, a mandatory OTP of Keycloak and the
    ``platform-beheer`` client role (the ``zad-beheer`` client in ``bootstrap.yaml``). It
    opens exactly the same session as the normal login, so being a platform administrator
    still hangs on ``is_platform_admin`` and the step-up of ZAD still applies.

    Not in the menu, and open in the normal state as well; see
    ``features/sso-rijk-uitwijk.md`` for what guards it.
    """
    oauth = request.app.state.oauth

    if not hasattr(oauth, "beheer"):
        logger.error("Break-glass login unavailable: the zad-beheer OAuth client is not registered")
        raise HTTPException(status_code=503, detail="De noodweg is op dit cluster niet beschikbaar.")

    redirect_uri = str(request.url_for("beheer_callback"))
    logger.info(f"Break-glass login started with redirect URI: {redirect_uri}")
    return await oauth.beheer.authorize_redirect(request, redirect_uri)


@auth_router.get("/beheer/callback", name="beheer_callback")
async def beheer_callback(request: Request) -> Response:
    """The callback of the break-glass login; the same session as the normal one."""
    return await _finish_login(request, "beheer")


@auth_router.get("/callback")
async def auth_callback(request: Request) -> Response:
    """Exchange the authorization code from Keycloak for the user and open a session."""
    return await _finish_login(request, "keycloak")


async def _finish_login(request: Request, client_name: str) -> Response:
    """Exchange the authorization code for the user and open the portal session.

    Shared by the normal login and the break-glass login: both end in one session of the
    same kind, so there is one place where an identity becomes a session.

    The client is looked up INSIDE the try: an unregistered client (a startup that did not
    finish) is then a failed login like any other, and not a 500.
    """
    try:
        client: StarletteOAuth2App = getattr(request.app.state.oauth, client_name)
        token = await client.authorize_access_token(request)

        user_info = None

        if token.get("userinfo"):
            user_info = token["userinfo"]

        if not user_info:
            logger.error("No user info could be extracted from token response")
            raise HTTPException(status_code=500, detail="Failed to retrieve user information")

        logger.info(f"OAuth callback successful for user: {user_info.get('email', 'unknown')}")
        logger.debug(f"User info received: {list(user_info.keys())}")

        # Authorization keys entirely on the email claim (allowlist + admin gating), so an
        # unverified email must never establish a session: it would let a login impersonate
        # any allowed or admin user. BZK-federated users are trustEmail=true and carry
        # email_verified=true, so they pass.
        if user_info.get("email_verified") is not True:
            logger.warning(f"Refusing login: email_verified is not true for {user_info.get('email', 'unknown')}")
            # The reason travels server-side as a one-shot flash and not as a query parameter.
            # No "user" is stored, so this grants nothing; it only tells the denied page why.
            request.session["denied_reason"] = "email-not-verified"
            return RedirectResponse(url="/permission-denied", status_code=302)

        # Session fixation: a new session id on the privilege hinge, so a planted id (cookie
        # tossing from a sibling subdomain) is dead before the identity lands in it.
        session = request.session
        if isinstance(session, ServerSideSession):
            await session.regenerate()

        request.session["user"] = {
            "sub": user_info.get("sub"),
            "email": user_info.get("email"),
            "name": user_info.get("name", user_info.get("preferred_username", "Unknown")),
            "given_name": user_info.get("given_name"),
            "family_name": user_info.get("family_name"),
            "preferred_username": user_info.get("preferred_username"),
            # The moment of authentication, read by the absolute session lifetime and by revocation.
            "login_at": time.time(),
        }

        # Keycloak's end-session endpoint wants this token back on logout.
        if token.get("id_token"):
            request.session["id_token"] = token["id_token"]

        user_service = get_user_service()
        user_service.store_user(request.session["user"])

        return RedirectResponse(url="/dashboard", status_code=302)

    except OAuthError as e:
        # A rejected or aborted login (invalid_scope, access_denied, a stale callback) is a
        # user-side condition and not a server fault, so it warns instead of raising.
        error_description = getattr(e, "description", "Unknown OAuth error")
        error_code = getattr(e, "error", "unknown_error")
        logger.warning(f"OAuth callback failed: {error_code} - {error_description}")

        error_param = quote_plus(f"OAuth error: {error_description}")
        return RedirectResponse(url=f"/?error={error_param}", status_code=302)

    except Exception as e:  # noqa: BLE001  # the outer boundary of this request; it must still answer
        logger.error(f"Unexpected error during OAuth callback: {e}")
        error_param = quote_plus("Authentication failed. Please try again.")
        return RedirectResponse(url=f"/?error={error_param}", status_code=302)


@auth_router.get("/logout")
async def logout(request: Request) -> Response:
    """Log the user out here, and at Keycloak's end-session endpoint when there is one."""
    try:
        user = request.session.get("user")
        id_token = request.session.get("id_token")
        user_email = user.get("email", "unknown") if user else "anonymous"

        logger.info(f"Logging out user: {user_email}")

        # Clearing the cookie only ends the session in this browser, so every session this
        # user has open elsewhere is revoked as well.
        if user and user.get("email"):
            user_service = get_user_service()
            user_service.remove_user(user["email"])
            try:
                await revoke_sessions(user["email"])
            except SQLAlchemyError as e:
                # A database fault may not break off the logout: the end-session call below
                # would be skipped and the SSO session would stay open.
                logger.error(f"Sessies van {user['email']} niet ingetrokken: {e}")

        request.session.clear()

        oauth = request.app.state.oauth
        if hasattr(oauth, "keycloak"):
            metadata = await oauth.keycloak.load_server_metadata()
            end_session_url = metadata.get("end_session_endpoint")
            if end_session_url:
                post_logout_uri = str(request.base_url)
                params = {"post_logout_redirect_uri": post_logout_uri}
                if id_token:
                    params["id_token_hint"] = id_token
                query = "&".join(f"{k}={quote_plus(v)}" for k, v in params.items())
                logout_url = f"{end_session_url}?{query}"
                logger.info("Redirecting to Keycloak end-session endpoint for full SSO logout")
                return RedirectResponse(url=logout_url, status_code=302)

        logger.info("User logged out successfully (local session only)")
        return RedirectResponse(url="/", status_code=302)

    except Exception as e:  # noqa: BLE001  # the outer boundary of this request; it must still answer
        logger.error(f"Error during logout: {e}")
        # The session goes even when the logout failed.
        request.session.clear()
        return RedirectResponse(url="/", status_code=302)


# Called by hand, not by a frontend or by code: docs/keycloak/opi-sso-setup.md uses it to
# check the SSO attributes. response_class=JSONResponse because auth_router inherits the
# app's HTMLResponse default, which would .encode() this dict and raise a 500.
@auth_router.get("/user", response_class=JSONResponse)
async def get_current_user_info(request: Request) -> dict:
    """The user of the current session, as the session holds it."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    logger.debug(f"Retrieved user info for: {user.get('email', 'unknown')}")
    return user
