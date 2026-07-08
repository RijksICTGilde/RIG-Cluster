"""
Authentication routes for Keycloak SSO integration.

This module provides the login, logout, and OAuth callback endpoints
for handling user authentication via Keycloak.
"""

import logging
from typing import TYPE_CHECKING
from urllib.parse import quote_plus

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from starlette.responses import Response

logger = logging.getLogger(__name__)

auth_router = APIRouter(prefix="/auth", tags=["authentication"])


@auth_router.get("/login")
async def login(request: Request) -> Response:
    """
    Initiate the OAuth login flow with Keycloak.

    This endpoint starts the OAuth authorization flow by redirecting
    the user to Keycloak's authorization endpoint.

    Args:
        request: The FastAPI request object

    Returns:
        Redirect response to Keycloak authorization endpoint
    """
    try:
        # Get the OAuth client from the app state
        oauth = request.app.state.oauth

        # OAuth client should always be registered after startup completes
        if not hasattr(oauth, "keycloak"):
            raise HTTPException(
                status_code=500, detail="Authentication system not initialized - startup may have failed"
            )

        keycloak = oauth.keycloak

        # Build the redirect URI for the callback
        redirect_uri = str(request.url_for("auth_callback"))
        logger.info(f"Initiating OAuth login with redirect URI: {redirect_uri}")

        # Generate the authorization URL and redirect the user
        return await keycloak.authorize_redirect(request, redirect_uri)

    except Exception as e:
        logger.error(f"Error initiating OAuth login: {e}")

        # Add more context about what might be causing DNS resolution errors
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

        # Don't catch and swallow the exception - let it bubble up with more context
        raise HTTPException(status_code=500, detail=f"OAuth login failed: {e!s}")


@auth_router.get("/callback")
async def auth_callback(request: Request) -> Response:
    """
    Handle the OAuth callback from Keycloak.

    This endpoint processes the authorization code returned by Keycloak
    and exchanges it for user information.

    Args:
        request: The FastAPI request object containing the authorization code

    Returns:
        Redirect response to the dashboard or original destination
    """
    try:
        # Get the OAuth client from the app state
        oauth = request.app.state.oauth
        keycloak = oauth.keycloak

        # Exchange the authorization code for an access token and user info
        token = await keycloak.authorize_access_token(request)

        # Get user info from the token response
        user_info = None

        if token.get("userinfo"):
            user_info = token["userinfo"]

        if not user_info:
            logger.error("No user info could be extracted from token response")
            raise HTTPException(status_code=500, detail="Failed to retrieve user information")

        logger.info(f"OAuth callback successful for user: {user_info.get('email', 'unknown')}")
        logger.debug(f"User info received: {list(user_info.keys())}")

        # Store user information in the session
        request.session["user"] = {
            "sub": user_info.get("sub"),
            "email": user_info.get("email"),
            "name": user_info.get("name", user_info.get("preferred_username", "Unknown")),
            "given_name": user_info.get("given_name"),
            "family_name": user_info.get("family_name"),
            "preferred_username": user_info.get("preferred_username"),
        }

        # Store id_token for proper Keycloak logout
        if token.get("id_token"):
            request.session["id_token"] = token["id_token"]

        # Store the user in our user service
        user_service = get_user_service()
        user_service.store_user(request.session["user"])

        # Redirect to dashboard after successful login
        return RedirectResponse(url="/dashboard", status_code=302)

    except OAuthError as e:
        # A rejected or aborted login (invalid_scope, access_denied, stale callback,
        # ...) is a handled user-side condition, not a server fault - log one concise
        # warning and redirect the user to a friendly error page.
        error_description = getattr(e, "description", "Unknown OAuth error")
        error_code = getattr(e, "error", "unknown_error")
        logger.warning(f"OAuth callback failed: {error_code} - {error_description}")

        # Redirect to login page with error message
        error_param = quote_plus(f"OAuth error: {error_description}")
        return RedirectResponse(url=f"/?error={error_param}", status_code=302)

    except Exception as e:
        logger.error(f"Unexpected error during OAuth callback: {e}")
        error_param = quote_plus("Authentication failed. Please try again.")
        return RedirectResponse(url=f"/?error={error_param}", status_code=302)


@auth_router.get("/logout")
async def logout(request: Request) -> Response:
    """
    Log out the current user.

    This endpoint clears the user session and optionally redirects
    to Keycloak's logout endpoint for complete SSO logout.

    Args:
        request: The FastAPI request object

    Returns:
        Redirect response to the home page or Keycloak logout
    """
    try:
        # Get current user info before clearing session
        user = request.session.get("user")
        id_token = request.session.get("id_token")
        user_email = user.get("email", "unknown") if user else "anonymous"

        logger.info(f"Logging out user: {user_email}")

        # Remove user from our user service
        if user and user.get("email"):
            user_service = get_user_service()
            user_service.remove_user(user["email"])

        # Clear the session
        request.session.clear()

        # Redirect to Keycloak's end-session endpoint for full SSO logout
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

    except Exception as e:
        logger.error(f"Error during logout: {e}")
        # Even if logout fails, clear the session and redirect
        request.session.clear()
        return RedirectResponse(url="/", status_code=302)


@auth_router.get("/user")
async def get_current_user_info(request: Request) -> dict:
    """
    Get information about the currently authenticated user.

    This is a utility endpoint for debugging and frontend use.

    Args:
        request: The FastAPI request object

    Returns:
        User information dictionary
    """
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    logger.debug(f"Retrieved user info for: {user.get('email', 'unknown')}")
    return user
