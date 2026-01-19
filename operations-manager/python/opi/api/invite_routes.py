"""
Invite routes for handling user invitations to project realms.

These routes are PUBLIC and do not require SSO authentication.
They provide the invite flow for users to join a project's Keycloak realm.
"""

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from opi.core.templates import get_templates
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.manager.invite_manager import (
    InviteAuthMethodError,
    InviteDomainError,
    InviteError,
    InviteExpiredError,
    InviteManager,
    UserExistsError,
)
from opi.services.project_service import get_project_service
from opi.utils.naming import generate_project_realm_name
from starlette.responses import Response

logger = logging.getLogger(__name__)

invite_router = APIRouter(prefix="/invite", tags=["invites"])


def _find_project_by_invite_key(key: str) -> tuple[str, dict[str, Any], dict[str, Any], str] | None:
    """
    Search all projects in memory for an invite with the given key.

    Args:
        key: The invite key to search for

    Returns:
        Tuple of (project_name, project_data, invite, cluster) or None if not found
    """
    project_service = get_project_service()
    handler = ProjectFileHandler()

    # Search all projects in memory
    all_projects = project_service.get_all_projects()

    for project_name, project in all_projects.items():
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
            "invite_expired": "Uitnodiging verlopen",
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
            "generic_error": "Er is een fout opgetreden",
        },
        "en": {
            "invite_not_found": "Invitation not found",
            "invite_expired": "Invitation expired",
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
            "generic_error": "An error occurred",
        },
    }
    return messages.get(language, messages["nl"])


@invite_router.get("/{key}", response_class=HTMLResponse)
async def invite_landing(request: Request, key: str) -> Response:
    """
    Display the invite landing page with authentication options.

    Args:
        request: FastAPI request
        key: The invite key

    Returns:
        HTML response with landing page
    """
    templates = get_templates()

    # Find project and invite
    result = _find_project_by_invite_key(key)
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

    # Validate invite
    invite_manager = InviteManager()
    try:
        invite_manager.validate_invite(project_data, invite)
    except InviteExpiredError as e:
        error_messages = _get_error_messages(language)
        return templates.TemplateResponse(
            "invite-error.html.j2",
            {
                "request": request,
                "error_title": error_messages["invite_expired"],
                "error_message": f"Verlopen op: {e.expired_at}" if language == "nl" else f"Expired on: {e.expired_at}",
                "language": language,
            },
        )

    # Get auth methods
    auth_methods = invite_manager.project_file_handler.get_invite_auth_methods(project_data, invite)

    # Get localized content
    message = invite_manager.project_file_handler.get_invite_message(invite, language)
    display_name = project_data.get("display-name", project_name)

    return templates.TemplateResponse(
        "invite-landing.html.j2",
        {
            "request": request,
            "project_name": project_name,
            "display_name": display_name,
            "invite_key": key,
            "message": message,
            "allow_sso": auth_methods["sso"],
            "allow_local": auth_methods["local"],
            "language": language,
        },
    )


@invite_router.get("/{key}/sso")
async def invite_sso_start(request: Request, key: str) -> Response:
    """
    Initiate the SSO authentication flow for an invite.

    Stores the invite key in session and redirects to Keycloak.

    Args:
        request: FastAPI request
        key: The invite key

    Returns:
        Redirect to Keycloak authorization endpoint
    """
    # Find and validate invite
    result = _find_project_by_invite_key(key)
    if not result:
        raise HTTPException(status_code=404, detail="Invite not found")

    project_name, project_data, invite, cluster = result
    invite_manager = InviteManager()

    try:
        invite_manager.validate_invite(project_data, invite)
        invite_manager.validate_auth_method(project_data, invite, "sso")
    except InviteError as e:
        raise HTTPException(status_code=400, detail=e.message) from e

    # Store invite flow state in session
    request.session["invite_flow"] = {
        "key": key,
        "project_name": project_name,
        "cluster": cluster,
    }

    # Get OAuth client and redirect
    oauth = request.app.state.oauth

    if not hasattr(oauth, "keycloak"):
        raise HTTPException(status_code=500, detail="Authentication system not initialized")

    callback_url = str(request.url_for("invite_sso_callback", key=key))
    logger.info(f"Starting invite SSO flow for key '{key}', callback: {callback_url}")

    return await oauth.keycloak.authorize_redirect(request, callback_url)


@invite_router.get("/{key}/sso/callback")
async def invite_sso_callback(request: Request, key: str) -> Response:
    """
    Handle the OAuth callback after SSO authentication.

    Completes the invite flow by creating/updating the user and assigning permissions.

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

    # Find project and invite
    result = _find_project_by_invite_key(key)
    if not result:
        return RedirectResponse(url=f"/invite/{key}/error?code=invite_not_found", status_code=302)

    project_name, project_data, invite, cluster = result

    try:
        # Exchange token
        oauth = request.app.state.oauth
        token = await oauth.keycloak.authorize_access_token(request)

        user_info = token.get("userinfo")
        if not user_info:
            raise InviteError("Failed to retrieve user information", "no_user_info")

        logger.info(f"SSO callback for invite '{key}', user: {user_info.get('email', 'unknown')}")

        # Complete invite flow
        invite_manager = InviteManager()
        realm_name = generate_project_realm_name(project_name, cluster)

        result_data = await invite_manager.complete_sso_invite(
            project_data=project_data,
            project_name=project_name,
            invite=invite,
            user_info=user_info,
            realm_name=realm_name,
        )

        # Clear session state
        request.session.pop("invite_flow", None)

        # Store success info in session for success page
        request.session["invite_success"] = {
            "email": result_data["email"],
            "created": result_data["created"],
            "assigned": result_data["assigned"],
        }

        return RedirectResponse(url=f"/invite/{key}/success", status_code=302)

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
    result = _find_project_by_invite_key(key)
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
        invite_manager.validate_invite(project_data, invite)
        invite_manager.validate_auth_method(project_data, invite, "local")
    except InviteExpiredError as e:
        return templates.TemplateResponse(
            "invite-error.html.j2",
            {
                "request": request,
                "error_title": error_messages["invite_expired"],
                "error_message": f"Verlopen op: {e.expired_at}" if language == "nl" else f"Expired on: {e.expired_at}",
                "language": language,
            },
        )
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
    result = _find_project_by_invite_key(key)
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
        invite_manager.validate_invite(project_data, invite)
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

    # Validate password confirmation
    errors: dict[str, str] = {}
    if form_data["password"] != password_confirm:
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
    except Exception as e:
        logger.exception(f"Error creating local account for invite '{key}'")
        return templates.TemplateResponse(
            "invite-error.html.j2",
            {
                "request": request,
                "error_title": error_messages["generic_error"],
                "error_message": str(e),
                "language": language,
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
    result = _find_project_by_invite_key(key)
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

    invite_manager = InviteManager()

    # Get localized content
    success_title = invite_manager.project_file_handler.get_invite_success_title(invite, language)
    success_button = invite_manager.project_file_handler.get_invite_success_button(invite, language)
    application_url = invite.get("application_url", "")
    display_name = project_data.get("display-name", project_name)

    # Get success info from session
    success_info = request.session.pop("invite_success", {})

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
    result = _find_project_by_invite_key(key)
    if result:
        _, project_data, _, _ = result
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
