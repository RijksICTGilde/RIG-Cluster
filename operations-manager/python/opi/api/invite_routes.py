"""
Invite routes for handling user invitations to project realms.

These routes are PUBLIC and do not require SSO authentication.
They provide the invite flow for users to join a project's Keycloak realm.
"""

import base64
import hashlib
import logging
import re
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from opi.core.config import settings
from opi.core.templates import get_templates
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.invite_manager import (
    InviteAuthMethodError,
    InviteDomainError,
    InviteError,
    InviteManager,
    UserExistsError,
)
from opi.services.project_store import get_project_store
from opi.utils.naming import generate_project_realm_name
from starlette.responses import Response

logger = logging.getLogger(__name__)


def _generate_pkce_pair() -> tuple[str, str]:
    """
    Generate PKCE code_verifier and code_challenge pair.

    Returns:
        Tuple of (code_verifier, code_challenge)
    """
    # Generate random code_verifier (43-128 characters)
    code_verifier = secrets.token_urlsafe(32)

    # Create code_challenge using S256 method
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    return code_verifier, code_challenge


def _get_keycloak_url_for_cluster(cluster: str) -> str:
    """
    Get the Keycloak URL for a given cluster.

    Args:
        cluster: The cluster name

    Returns:
        Keycloak base URL
    """
    # Import here to avoid circular imports
    from opi.core.cluster_config import get_keycloak_discovery_url

    return get_keycloak_discovery_url(cluster)


def _build_realm_auth_url(
    keycloak_url: str,
    realm_name: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    idp_hint: str | None = None,
) -> str:
    """
    Build the Keycloak authorization URL for a project realm.

    Args:
        keycloak_url: Base Keycloak URL
        realm_name: The project realm name
        redirect_uri: OAuth callback URL
        state: OAuth state parameter
        code_challenge: PKCE code challenge
        idp_hint: Optional IDP alias to skip login screen and go directly to IDP

    Returns:
        Full authorization URL
    """
    params = {
        "client_id": settings.INVITE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "login",  # Force login screen even if user has existing Keycloak session
    }

    # Add kc_idp_hint to skip Keycloak login screen and go directly to the IDP
    if idp_hint:
        params["kc_idp_hint"] = idp_hint

    auth_endpoint = f"{keycloak_url}/realms/{realm_name}/protocol/openid-connect/auth"
    return f"{auth_endpoint}?{urlencode(params)}"


async def _exchange_code_for_token(
    keycloak_url: str,
    realm_name: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    """
    Exchange authorization code for tokens.

    Args:
        keycloak_url: Base Keycloak URL
        realm_name: The project realm name
        code: Authorization code from callback
        redirect_uri: OAuth callback URL (must match original)
        code_verifier: PKCE code verifier

    Returns:
        Token response dict

    Raises:
        httpx.HTTPStatusError: If token exchange fails
    """
    token_endpoint = f"{keycloak_url}/realms/{realm_name}/protocol/openid-connect/token"

    data = {
        "grant_type": "authorization_code",
        "client_id": settings.INVITE_CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return response.json()


async def _get_userinfo(keycloak_url: str, realm_name: str, access_token: str) -> dict[str, Any]:
    """
    Get user info from Keycloak using access token.

    Args:
        keycloak_url: Base Keycloak URL
        realm_name: The project realm name
        access_token: OAuth access token

    Returns:
        User info dict

    Raises:
        httpx.HTTPStatusError: If userinfo request fails
    """
    userinfo_endpoint = f"{keycloak_url}/realms/{realm_name}/protocol/openid-connect/userinfo"

    async with httpx.AsyncClient() as client:
        response = await client.get(
            userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()


invite_router = APIRouter(prefix="/invite", tags=["invites"])


async def _find_project_by_invite_key(key: str) -> tuple[str, dict[str, Any], dict[str, Any], str] | None:
    """
    Search all projects in memory for an invite with the given key.

    Ensures project data is fresh (refreshes from Git if stale, max every 30 seconds).

    Args:
        key: The invite key to search for

    Returns:
        Tuple of (project_name, project_data, invite, cluster) or None if not found
    """
    # Ensure project data is fresh before searching

    handler = ProjectFileHandler()

    # Search all projects in memory
    all_projects = get_project_store().get_all()

    for project in all_projects:
        project_name = project.name
        project_data = project.data
        if not project_data:
            continue

        invite = handler.get_invite_by_key(project_data, key)
        if invite:
            # Get the cluster from the first deployment
            deployments: list[Any] = project_data.get("deployments", [])
            cluster: str = "local"
            if deployments and len(deployments) > 0:
                first_deployment: Any = deployments[0]
                if isinstance(first_deployment, dict):
                    cluster_value: Any = first_deployment.get("cluster", "local")
                    cluster = str(cluster_value) if cluster_value else "local"

            logger.debug(f"Found invite key '{key}' in project '{project_name}' (cluster: {cluster})")
            return project_name, project_data, invite, cluster

    logger.debug(f"Invite key '{key}' not found in any project")
    return None


def _get_language(request: Request, project_data: dict[str, Any]) -> str:
    """
    Determine the language for the invite page.

    Args:
        request: FastAPI request
        project_data: Project configuration data

    Returns:
        Two-letter language code
    """
    invite_manager = InviteManager()
    settings_dict = invite_manager.project_file_handler.get_invite_settings(project_data)
    default_lang = settings_dict.get("default_language", "nl")

    return invite_manager.detect_language(
        request.query_params.get("lang"),
        request.headers.get("accept-language"),
        default_lang,
    )


def _get_error_messages(language: str) -> dict[str, str]:
    """
    Get localized error messages.

    Args:
        language: Two-letter language code

    Returns:
        Dict of error code to message
    """
    messages = {
        "nl": {
            "invite_not_found": "Uitnodiging niet gevonden",
            "domain_mismatch": "E-mailadres komt niet overeen met vereiste domein",
            "auth_method_not_allowed": "Authenticatiemethode niet beschikbaar voor deze uitnodiging",
            "user_exists": "Account bestaat al. Gebruik SSO login.",
            "missing_email": "E-mailadres is verplicht",
            "missing_first_name": "Voornaam is verplicht",
            "missing_last_name": "Achternaam is verplicht",
            "missing_password": "Wachtwoord is verplicht",
            "invalid_email": "Ongeldig e-mailadres",
            "password_too_short": "Wachtwoord moet minimaal 12 tekens bevatten",
            "password_no_uppercase": "Wachtwoord moet minimaal een hoofdletter bevatten",
            "password_no_lowercase": "Wachtwoord moet minimaal een kleine letter bevatten",
            "password_no_digit": "Wachtwoord moet minimaal een cijfer bevatten",
            "password_mismatch": "Wachtwoorden komen niet overeen",
            "role_not_assigned": "Account aangemaakt, maar rol niet toegekend",
            "generic_error": "Er is een fout opgetreden",
        },
        "en": {
            "invite_not_found": "Invitation not found",
            "domain_mismatch": "Email address does not match required domain",
            "auth_method_not_allowed": "Authentication method not available for this invitation",
            "user_exists": "Account already exists. Use SSO login.",
            "missing_email": "Email address is required",
            "missing_first_name": "First name is required",
            "missing_last_name": "Last name is required",
            "missing_password": "Password is required",
            "invalid_email": "Invalid email address",
            "password_too_short": "Password must be at least 12 characters",
            "password_no_uppercase": "Password must contain at least one uppercase letter",
            "password_no_lowercase": "Password must contain at least one lowercase letter",
            "password_no_digit": "Password must contain at least one digit",
            "password_mismatch": "Passwords do not match",
            "role_not_assigned": "Account created, but role not assigned",
            "generic_error": "An error occurred",
        },
    }
    return messages.get(language, messages["nl"])


def _realm_roles_unassigned(assigned: dict[str, Any]) -> bool:
    """Whether the redemption assigned a realm user but could NOT grant a NAMED realm role.

    ``assign_invite_permissions`` records a ``Realm roles not found: ...`` error only when the
    invite actually requested realm roles that Keycloak did not have; a deliberately role-less
    invite never attempts an assignment, so it never trips this. When it is true the user has an
    account without the intended role, hits the authorization wall, and retrying loops on
    ``UserExistsError`` -- so the flow must show an error page (decision 11) instead of success.
    """
    errors = assigned.get("errors") if isinstance(assigned, dict) else None
    if not isinstance(errors, list):
        return False
    return any(isinstance(msg, str) and msg.startswith("Realm roles not found") for msg in errors)


@invite_router.get("/{key}", response_class=HTMLResponse)
async def invite_landing(request: Request, key: str) -> Response:
    """
    Display the invite landing page with authentication options.

    Queries Keycloak to determine which authentication methods are actually
    available in the project's realm.

    Args:
        request: FastAPI request
        key: The invite key

    Returns:
        HTML response with landing page
    """
    templates = get_templates()

    # Find project and invite
    result = await _find_project_by_invite_key(key)
    if not result:
        return templates.TemplateResponse(
            "invite-error.html.j2",
            {
                "request": request,
                "error_title": "Uitnodiging niet gevonden",
                "error_message": "De opgegeven uitnodiging bestaat niet of is niet meer geldig.",
                "language": "nl",
            },
        )

    project_name, project_data, invite, cluster = result
    language = _get_language(request, project_data)

    invite_manager = InviteManager()

    # Get the project's realm name
    realm_name = generate_project_realm_name(project_name, cluster)

    # Query Keycloak for actual available auth methods in this realm
    realm_auth = await invite_manager.get_realm_auth_methods(realm_name)

    # Combine with invite-level restrictions (invite can restrict, not expand)
    invite_auth_config = invite_manager.project_file_handler.get_invite_auth_methods(project_data, invite)

    # Final auth methods: must be available in realm AND allowed by invite config
    allow_sso = realm_auth["sso"] and invite_auth_config["sso"]
    allow_local = realm_auth["local"] and invite_auth_config["local"]

    # Get localized content
    message = invite_manager.project_file_handler.get_invite_message(invite, language)
    display_name = project_data.get("display-name", project_name)
    contact_email = invite.get("contact_email", "")

    # Get identity provider display names for SSO buttons
    identity_providers = realm_auth.get("identity_providers", [])

    return templates.TemplateResponse(
        "invite-landing.html.j2",
        {
            "request": request,
            "project_name": project_name,
            "display_name": display_name,
            "invite_key": key,
            "message": message,
            "allow_sso": allow_sso,
            "allow_local": allow_local,
            "identity_providers": identity_providers,
            "contact_email": contact_email,
            "language": language,
        },
    )


@invite_router.get("/{key}/sso")
async def invite_sso_start(request: Request, key: str) -> Response:
    """
    Initiate the SSO authentication flow for an invite.

    Redirects to the PROJECT's Keycloak realm for authentication.
    Uses PKCE with a public client for security.

    Args:
        request: FastAPI request
        key: The invite key

    Returns:
        Redirect to project realm's Keycloak authorization endpoint
    """
    # Find and validate invite
    result = await _find_project_by_invite_key(key)
    if not result:
        raise HTTPException(status_code=404, detail="Invite not found")

    project_name, project_data, invite, cluster = result
    invite_manager = InviteManager()

    try:
        invite_manager.validate_auth_method(project_data, invite, "sso")
    except InviteError as e:
        raise HTTPException(status_code=400, detail=e.message) from e

    # Generate PKCE pair for secure public client flow
    code_verifier, code_challenge = _generate_pkce_pair()

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(16)

    # Get project realm info
    realm_name = generate_project_realm_name(project_name, cluster)
    keycloak_url = _get_keycloak_url_for_cluster(cluster)

    # Build callback URL
    callback_url = str(request.url_for("invite_sso_callback", key=key))

    # Store invite flow state in session (including PKCE verifier)
    request.session["invite_flow"] = {
        "key": key,
        "project_name": project_name,
        "cluster": cluster,
        "realm_name": realm_name,
        "keycloak_url": keycloak_url,
        "code_verifier": code_verifier,
        "state": state,
        "redirect_uri": callback_url,
    }

    # Build authorization URL for the project's realm
    auth_url = _build_realm_auth_url(
        keycloak_url=keycloak_url,
        realm_name=realm_name,
        redirect_uri=callback_url,
        state=state,
        code_challenge=code_challenge,
    )

    logger.info(f"Starting invite SSO flow for key '{key}', realm: {realm_name}, callback: {callback_url}")

    return RedirectResponse(url=auth_url, status_code=302)


@invite_router.get("/{key}/idp/{idp_alias}")
async def invite_idp_start(request: Request, key: str, idp_alias: str) -> Response:
    """
    Initiate SSO authentication flow for a specific identity provider.

    Redirects directly to the chosen IDP, skipping the Keycloak login screen.
    Uses kc_idp_hint to tell Keycloak which IDP to use.

    Args:
        request: FastAPI request
        key: The invite key
        idp_alias: The Keycloak identity provider alias (e.g., 'sso-rijk')

    Returns:
        Redirect to project realm's Keycloak authorization endpoint with IDP hint
    """
    # Find and validate invite
    result = await _find_project_by_invite_key(key)
    if not result:
        raise HTTPException(status_code=404, detail="Invite not found")

    project_name, project_data, invite, cluster = result
    invite_manager = InviteManager()

    try:
        invite_manager.validate_auth_method(project_data, invite, "sso")
    except InviteError as e:
        raise HTTPException(status_code=400, detail=e.message) from e

    # Validate that the IDP alias exists in the realm
    realm_name = generate_project_realm_name(project_name, cluster)
    realm_auth = await invite_manager.get_realm_auth_methods(realm_name)
    identity_providers = realm_auth.get("identity_providers", [])

    valid_aliases = [idp["alias"] for idp in identity_providers]
    if idp_alias not in valid_aliases:
        logger.warning(f"Invalid IDP alias '{idp_alias}' for invite '{key}'. Valid: {valid_aliases}")
        raise HTTPException(status_code=404, detail=f"Identity provider '{idp_alias}' not found")

    # Generate PKCE pair for secure public client flow
    code_verifier, code_challenge = _generate_pkce_pair()

    # Generate state for CSRF protection
    state = secrets.token_urlsafe(16)

    # Get project realm info
    keycloak_url = _get_keycloak_url_for_cluster(cluster)

    # Build callback URL (reuse the same callback as regular SSO)
    callback_url = str(request.url_for("invite_sso_callback", key=key))

    # Store invite flow state in session (including PKCE verifier)
    request.session["invite_flow"] = {
        "key": key,
        "project_name": project_name,
        "cluster": cluster,
        "realm_name": realm_name,
        "keycloak_url": keycloak_url,
        "code_verifier": code_verifier,
        "state": state,
        "redirect_uri": callback_url,
    }

    # Build authorization URL with IDP hint to skip login screen
    auth_url = _build_realm_auth_url(
        keycloak_url=keycloak_url,
        realm_name=realm_name,
        redirect_uri=callback_url,
        state=state,
        code_challenge=code_challenge,
        idp_hint=idp_alias,
    )

    logger.info(f"Starting invite IDP flow for key '{key}', realm: {realm_name}, idp: {idp_alias}")

    return RedirectResponse(url=auth_url, status_code=302)


@invite_router.get("/{key}/sso/callback")
async def invite_sso_callback(request: Request, key: str) -> Response:
    """
    Handle the OAuth callback after SSO authentication.

    Exchanges the authorization code for tokens using the project realm,
    then completes the invite flow by assigning permissions.

    Args:
        request: FastAPI request
        key: The invite key

    Returns:
        Redirect to success or error page
    """
    # Verify session state
    invite_flow = request.session.get("invite_flow")
    if not invite_flow or invite_flow.get("key") != key:
        logger.warning(f"SSO callback session mismatch for key '{key}'")
        return RedirectResponse(url=f"/invite/{key}/error?code=session_error", status_code=302)

    # Verify state parameter (CSRF protection)
    state_param = request.query_params.get("state")
    if state_param != invite_flow.get("state"):
        logger.warning(f"SSO callback state mismatch for key '{key}'")
        return RedirectResponse(url=f"/invite/{key}/error?code=session_error", status_code=302)

    # Check for error from Keycloak
    error = request.query_params.get("error")
    if error:
        error_description = request.query_params.get("error_description", error)
        logger.warning(f"SSO callback error for key '{key}': {error} - {error_description}")
        return RedirectResponse(url=f"/invite/{key}/error?code=sso_error", status_code=302)

    # Get authorization code
    code = request.query_params.get("code")
    if not code:
        logger.warning(f"SSO callback missing code for key '{key}'")
        return RedirectResponse(url=f"/invite/{key}/error?code=missing_code", status_code=302)

    # Find project and invite
    result = await _find_project_by_invite_key(key)
    if not result:
        return RedirectResponse(url=f"/invite/{key}/error?code=invite_not_found", status_code=302)

    project_name, project_data, invite, cluster = result

    try:
        # Get stored OAuth parameters from session
        keycloak_url = invite_flow["keycloak_url"]
        realm_name = invite_flow["realm_name"]
        code_verifier = invite_flow["code_verifier"]
        redirect_uri = invite_flow["redirect_uri"]

        # Exchange code for tokens using project realm
        token_response = await _exchange_code_for_token(
            keycloak_url=keycloak_url,
            realm_name=realm_name,
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )

        access_token = token_response.get("access_token")
        if not access_token:
            raise InviteError("Failed to obtain access token", "token_error")

        # Get user info from project realm
        user_info = await _get_userinfo(keycloak_url, realm_name, access_token)

        if not user_info or not user_info.get("email"):
            raise InviteError("Failed to retrieve user information", "no_user_info")

        logger.info(f"SSO callback for invite '{key}', user: {user_info.get('email', 'unknown')}")

        # Complete invite flow - user is already authenticated in the project realm
        # We just need to assign the invite's roles and groups
        invite_manager = InviteManager()

        result_data = await invite_manager.complete_sso_invite(
            project_data=project_data,
            project_name=project_name,
            invite=invite,
            user_info=user_info,
            realm_name=realm_name,
        )

        # Clear session state
        request.session.pop("invite_flow", None)

        # The account was created but a named realm role could not be granted: show an error
        # page (decision 11), not success -- retrying loops on UserExistsError.
        if _realm_roles_unassigned(result_data["assigned"]):
            return RedirectResponse(url=f"/invite/{key}/error?code=role_not_assigned", status_code=302)

        # Store success info in session for success page
        request.session["invite_success"] = {
            "email": result_data["email"],
            "created": result_data["created"],
            "assigned": result_data["assigned"],
        }

        return RedirectResponse(url=f"/invite/{key}/success", status_code=302)

    except httpx.HTTPStatusError as e:
        logger.exception(f"HTTP error in SSO callback for invite '{key}': {e}")
        return RedirectResponse(url=f"/invite/{key}/error?code=token_error", status_code=302)
    except InviteDomainError as e:
        error_url = f"/invite/{key}/error?code=domain_mismatch&domain={e.required_domain}"
        return RedirectResponse(url=error_url, status_code=302)
    except InviteError as e:
        return RedirectResponse(url=f"/invite/{key}/error?code={e.error_code}", status_code=302)
    except Exception:
        logger.exception(f"Error in SSO callback for invite '{key}'")
        return RedirectResponse(url=f"/invite/{key}/error?code=generic_error", status_code=302)


@invite_router.get("/{key}/register", response_class=HTMLResponse)
async def invite_register_form(request: Request, key: str) -> Response:
    """
    Display the local account registration form.

    Args:
        request: FastAPI request
        key: The invite key

    Returns:
        HTML response with registration form
    """
    templates = get_templates()

    # Find and validate invite
    result = await _find_project_by_invite_key(key)
    if not result:
        return templates.TemplateResponse(
            "invite-error.html.j2",
            {
                "request": request,
                "error_title": "Uitnodiging niet gevonden",
                "error_message": "De opgegeven uitnodiging bestaat niet of is niet meer geldig.",
                "language": "nl",
            },
        )

    project_name, project_data, invite, _cluster = result
    language = _get_language(request, project_data)
    error_messages = _get_error_messages(language)

    invite_manager = InviteManager()

    try:
        invite_manager.validate_auth_method(project_data, invite, "local")
    except InviteAuthMethodError:
        if language == "nl":
            auth_error_msg = "Account aanmaken is niet beschikbaar voor deze uitnodiging."
        else:
            auth_error_msg = "Account creation is not available for this invitation."
        return templates.TemplateResponse(
            "invite-error.html.j2",
            {
                "request": request,
                "error_title": error_messages["auth_method_not_allowed"],
                "error_message": auth_error_msg,
                "language": language,
            },
        )

    display_name = project_data.get("display-name", project_name)
    message = invite_manager.project_file_handler.get_invite_message(invite, language)
    domain_restriction = invite.get("restrict_domain")

    return templates.TemplateResponse(
        "invite-register.html.j2",
        {
            "request": request,
            "project_name": project_name,
            "display_name": display_name,
            "invite_key": key,
            "message": message,
            "language": language,
            "domain_restriction": domain_restriction,
            "errors": {},
            "general_error": None,
            "form_data": None,
        },
    )


@invite_router.post("/{key}/register", response_class=HTMLResponse)
async def invite_register_submit(request: Request, key: str) -> Response:
    """
    Process the local account registration form submission.

    Args:
        request: FastAPI request
        key: The invite key

    Returns:
        Redirect to success page or re-render form with errors
    """
    templates = get_templates()

    # Find and validate invite
    result = await _find_project_by_invite_key(key)
    if not result:
        return templates.TemplateResponse(
            "invite-error.html.j2",
            {
                "request": request,
                "error_title": "Uitnodiging niet gevonden",
                "error_message": "De opgegeven uitnodiging bestaat niet of is niet meer geldig.",
                "language": "nl",
            },
        )

    project_name, project_data, invite, cluster = result
    language = _get_language(request, project_data)
    error_messages = _get_error_messages(language)

    invite_manager = InviteManager()

    try:
        invite_manager.validate_auth_method(project_data, invite, "local")
    except InviteError as e:
        return templates.TemplateResponse(
            "invite-error.html.j2",
            {
                "request": request,
                "error_title": error_messages.get(e.error_code, error_messages["generic_error"]),
                "error_message": e.message,
                "language": language,
            },
        )

    # Parse form data
    form = await request.form()
    form_data = {
        "email": str(form.get("email", "")).strip(),
        "first_name": str(form.get("first_name", "")).strip(),
        "last_name": str(form.get("last_name", "")).strip(),
        "password": str(form.get("password", "")),
    }
    password_confirm = str(form.get("password_confirm", ""))

    # Validate all fields and collect all errors
    errors: dict[str, str] = {}

    # Email validation
    if not form_data["email"]:
        errors["email"] = error_messages["missing_email"]
    else:
        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_pattern, form_data["email"]):
            errors["email"] = error_messages["invalid_email"]
        else:
            # Check domain restriction
            restrict_domain = invite.get("restrict_domain")
            if restrict_domain:
                domain = restrict_domain if restrict_domain.startswith("@") else f"@{restrict_domain}"
                if not form_data["email"].lower().endswith(domain.lower()):
                    errors["email"] = error_messages["domain_mismatch"]

    # Name validation
    if not form_data["first_name"]:
        errors["first_name"] = error_messages["missing_first_name"]
    if not form_data["last_name"]:
        errors["last_name"] = error_messages["missing_last_name"]

    # Password validation
    if not form_data["password"]:
        errors["password"] = error_messages["missing_password"]
    else:
        if len(form_data["password"]) < 12:
            errors["password"] = error_messages["password_too_short"]
        elif not re.search(r"[A-Z]", form_data["password"]):
            errors["password"] = error_messages["password_no_uppercase"]
        elif not re.search(r"[a-z]", form_data["password"]):
            errors["password"] = error_messages["password_no_lowercase"]
        elif not re.search(r"\d", form_data["password"]):
            errors["password"] = error_messages["password_no_digit"]

    # Password confirmation
    if (form_data["password"] and form_data["password"] != password_confirm) or not password_confirm:
        errors["password_confirm"] = error_messages["password_mismatch"]

    if errors:
        display_name = project_data.get("display-name", project_name)
        message = invite_manager.project_file_handler.get_invite_message(invite, language)
        return templates.TemplateResponse(
            "invite-register.html.j2",
            {
                "request": request,
                "project_name": project_name,
                "display_name": display_name,
                "invite_key": key,
                "message": message,
                "language": language,
                "domain_restriction": invite.get("restrict_domain"),
                "errors": errors,
                "form_data": form_data,
            },
        )

    try:
        realm_name = generate_project_realm_name(project_name, cluster)

        result_data = await invite_manager.complete_local_invite(
            project_data=project_data,
            project_name=project_name,
            invite=invite,
            form_data=form_data,
            realm_name=realm_name,
        )

        # Account created but a named realm role could not be granted: error page, not success
        # (decision 11) -- retrying loops on UserExistsError.
        if _realm_roles_unassigned(result_data["assigned"]):
            return RedirectResponse(url=f"/invite/{key}/error?code=role_not_assigned", status_code=302)

        # Store success info in session
        request.session["invite_success"] = {
            "email": result_data["email"],
            "created": result_data["created"],
            "assigned": result_data["assigned"],
        }

        return RedirectResponse(url=f"/invite/{key}/success", status_code=302)

    except (InviteDomainError, UserExistsError, InviteError) as e:
        # Map error to field
        field_map = {
            "domain_mismatch": "email",
            "user_exists": "email",
            "missing_email": "email",
            "invalid_email": "email",
            "missing_first_name": "first_name",
            "missing_last_name": "last_name",
            "missing_password": "password",
            "password_too_short": "password",
            "password_no_uppercase": "password",
            "password_no_lowercase": "password",
            "password_no_digit": "password",
        }
        field = field_map.get(e.error_code, "email")
        errors[field] = error_messages.get(e.error_code, e.message)

        # For user_exists, also show a general alert
        general_error = None
        if e.error_code == "user_exists":
            general_error = error_messages.get(e.error_code, e.message)

        display_name = project_data.get("display-name", project_name)
        return templates.TemplateResponse(
            "invite-register.html.j2",
            {
                "request": request,
                "project_name": project_name,
                "display_name": display_name,
                "invite_key": key,
                "language": language,
                "domain_restriction": invite.get("restrict_domain"),
                "errors": errors,
                "general_error": general_error,
                "form_data": form_data,
            },
        )
    except Exception:
        logger.exception(f"Error creating local account for invite '{key}'")
        display_name = project_data.get("display-name", project_name)
        return templates.TemplateResponse(
            "invite-register.html.j2",
            {
                "request": request,
                "project_name": project_name,
                "display_name": display_name,
                "invite_key": key,
                "language": language,
                "domain_restriction": invite.get("restrict_domain"),
                "errors": {},
                "general_error": error_messages["generic_error"],
                "form_data": form_data,
            },
        )


@invite_router.get("/{key}/success", response_class=HTMLResponse)
async def invite_success(request: Request, key: str) -> Response:
    """
    Display the success page after completing the invite flow.

    Args:
        request: FastAPI request
        key: The invite key

    Returns:
        HTML response with success page
    """
    templates = get_templates()

    # Find project and invite
    result = await _find_project_by_invite_key(key)
    if not result:
        return templates.TemplateResponse(
            "invite-error.html.j2",
            {
                "request": request,
                "error_title": "Uitnodiging niet gevonden",
                "error_message": "De opgegeven uitnodiging bestaat niet of is niet meer geldig.",
                "language": "nl",
            },
        )

    project_name, project_data, invite, _cluster = result
    language = _get_language(request, project_data)

    # Get success info from session - if empty, user accessed directly without completing flow
    success_info = request.session.pop("invite_success", {})
    if not success_info:
        # Redirect to landing page - user hasn't completed the invite flow
        return RedirectResponse(url=f"/invite/{key}", status_code=302)

    invite_manager = InviteManager()

    # Get localized content
    success_title = invite_manager.project_file_handler.get_invite_success_title(invite, language)
    success_button = invite_manager.project_file_handler.get_invite_success_button(invite, language)
    application_url = invite.get("application_url", "")
    display_name = project_data.get("display-name", project_name)

    return templates.TemplateResponse(
        "invite-success.html.j2",
        {
            "request": request,
            "project_name": project_name,
            "display_name": display_name,
            "success_title": success_title,
            "success_button": success_button,
            "application_url": application_url,
            "language": language,
            "email": success_info.get("email"),
            "created": success_info.get("created"),
            "assigned": success_info.get("assigned", {}),
        },
    )


@invite_router.get("/{key}/error", response_class=HTMLResponse)
async def invite_error(request: Request, key: str) -> Response:
    """
    Display an error page for invite-related errors.

    Args:
        request: FastAPI request
        key: The invite key

    Returns:
        HTML response with error page
    """
    templates = get_templates()

    # Try to find project for language detection
    result = await _find_project_by_invite_key(key)
    invite: dict[str, Any] = {}
    if result:
        _, project_data, invite, _ = result
        language = _get_language(request, project_data)
    else:
        language = request.query_params.get("lang", "nl")

    error_messages = _get_error_messages(language)
    error_code = request.query_params.get("code", "generic_error")
    domain = request.query_params.get("domain")

    error_title = error_messages.get(error_code, error_messages["generic_error"])

    # Build detailed error message
    if error_code == "domain_mismatch" and domain:
        if language == "nl":
            error_message = f"Uw e-mailadres moet eindigen op '{domain}'."
        else:
            error_message = f"Your email address must end with '{domain}'."
    elif error_code == "user_exists":
        if language == "nl":
            error_message = "Er bestaat al een account met dit e-mailadres. Gebruik de SSO login optie."
        else:
            error_message = "An account with this email already exists. Use the SSO login option."
    elif error_code == "role_not_assigned":
        # The account WAS created; only the intended role could not be granted. Say so
        # explicitly, warn that retrying will not work (it hits UserExistsError), and point at
        # the invite's contact address so the user knows who to ask (decision 11).
        contact = invite.get("contact_email")
        if language == "nl":
            error_message = (
                "Uw account is aangemaakt, maar de bijbehorende rol kon niet worden toegekend. "
                "Opnieuw proberen werkt niet: het account bestaat al. "
            )
            if contact:
                error_message += f"Neem contact op met {contact} om de rol alsnog te laten toekennen."
        else:
            error_message = (
                "Your account was created, but the intended role could not be assigned. "
                "Trying again will not work: the account already exists. "
            )
            if contact:
                error_message += f"Please contact {contact} to have the role assigned."
    else:
        error_message = ""

    return templates.TemplateResponse(
        "invite-error.html.j2",
        {
            "request": request,
            "error_title": error_title,
            "error_message": error_message,
            "language": language,
            "invite_key": key,
        },
    )
