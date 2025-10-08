"""
Authentication routes for Keycloak SSO integration.

This module provides the login, logout, and OAuth callback endpoints
for handling user authentication via Keycloak.
"""

import logging
from urllib.parse import quote_plus

from authlib.integrations.starlette_client import OAuthError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from opi.services.user_service import get_user_service
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

        # Add detailed logging to debug the DNS resolution issue
        from opi.core.config import settings

        logger.info("OAuth client configured with:")
        logger.info(f"  - client_id: {keycloak.client_id}")
        logger.info(f"  - client_secret: {'***' + keycloak.client_secret[-4:] if keycloak.client_secret else 'None'}")
        logger.info(f"  - discovery_url from settings: {settings.OIDC_DISCOVERY_URL}")
        logger.info(f"  - keycloak server metadata URL: {getattr(keycloak, 'server_metadata_url', 'Not available')}")

        # Generate the authorization URL and redirect the user
        return await keycloak.authorize_redirect(request, redirect_uri)

    except Exception as e:
        logger.error(f"Error initiating OAuth login: {e}")
        logger.error(f"Exception type: {type(e)}")
        logger.error(f"Exception details: {e!s}")

        # Add more context about what might be causing DNS resolution errors
        if "Name or service not known" in str(e):
            logger.error(
                "DNS resolution failed during OAuth login - this indicates the discovery URL cannot be resolved"
            )
            from opi.core.config import settings

            logger.error(f"OIDC_DISCOVERY_URL being used: {settings.OIDC_DISCOVERY_URL}")
            logger.error("Check if this URL is accessible from inside the Kubernetes pod")
            logger.error(
                "For production, it should typically be an external URL like: https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl/realms/rig-platform/.well-known/openid-configuration"
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

        # Log the current OAuth client configuration
        logger.info(f"OAuth callback - client_id: {keycloak.client_id}")
        logger.info(
            f"OAuth callback - client_secret: {'***' + keycloak.client_secret[-4:] if keycloak.client_secret else 'None'}"
        )
        # Note: server_metadata_url is not accessible from the OAuth client object directly

        # Log the incoming request parameters
        logger.info(f"OAuth callback - request params: {dict(request.query_params)}")
        logger.info(f"OAuth callback - request URL: {request.url}")

        # Exchange the authorization code for an access token and user info
        logger.info("OAuth callback - Starting token exchange...")
        token = await keycloak.authorize_access_token(request)
        logger.info("OAuth callback - Token exchange successful!")

        logger.info(f"Token received with keys: {list(token.keys()) if hasattr(token, 'keys') else type(token)}")

        # Get user info from the token response
        user_info = None

        if token.get("userinfo"):
            user_info = token["userinfo"]
            logger.info("Using pre-parsed userinfo from token response")

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

        # Store the user in our user service
        user_service = get_user_service()
        user_service.store_user(request.session["user"])

        # Redirect to dashboard after successful login
        return RedirectResponse(url="/dashboard", status_code=302)

    except OAuthError as e:
        logger.error(f"OAuth error during callback: {e}")
        logger.error(f"OAuth error type: {type(e)}")
        logger.error(f"OAuth error attributes: {dir(e)}")

        error_description = getattr(e, "description", "Unknown OAuth error")
        error_code = getattr(e, "error", "unknown_error")
        error_uri = getattr(e, "error_uri", "")

        logger.error(f"OAuth error details - code: {error_code}, description: {error_description}, uri: {error_uri}")

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
        user_email = user.get("email", "unknown") if user else "anonymous"

        logger.info(f"Logging out user: {user_email}")

        # Remove user from our user service
        if user and user.get("email"):
            user_service = get_user_service()
            user_service.remove_user(user["email"])

        # Clear the session
        request.session.clear()

        # For now, just redirect to the home page
        # In the future, we could implement full SSO logout with Keycloak
        logger.info("User logged out successfully")
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
