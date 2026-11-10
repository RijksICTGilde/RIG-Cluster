"""The invite flow a person walks to join the Keycloak realm of a project.

These routes are PUBLIC: they carry no SSO authentication, which is why every one of them
sits behind a rate limit.
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
from starlette.responses import Response

from opi.api.router import IPRateLimiter
from opi.core.config import settings
from opi.core.logging_redact import fingerprint_for_log, redact_invite_key_in_path
from opi.http.render.lotc_switch import render
from opi.manager.invite_manager import (
    InviteAuthMethodError,
    InviteDomainError,
    InviteError,
    InviteManager,
    UserExistsError,
)
from opi.services.catalog.invite.destination import resolve_invite_url
from opi.services.project_file_handler import ProjectFileHandler
from opi.services.project_store import get_project_store
from opi.utils.naming import generate_project_realm_name

logger = logging.getLogger(__name__)

#: Budget for one Keycloak call in the invite flow. Kept short: the invited person is
#: waiting on the page, and a token exchange answers in milliseconds.
KEYCLOAK_HTTP_TIMEOUT_SECONDS = 5.0


def _hourly_limiter(per_hour: int) -> IPRateLimiter:
    """A bucket that fills per HOUR, with the whole hourly limit as its burst."""
    return IPRateLimiter(requests_per_minute=per_hour / 60, burst=per_hour)


# A bearer secret without a limit is an account factory, so every route that looks a key up,
# on success and on failure alike, sits behind the read limit.
_READ_LIMITER = IPRateLimiter(requests_per_minute=30, burst=10)
_REGISTER_PER_MINUTE = IPRateLimiter(requests_per_minute=5, burst=5)
_REGISTER_PER_HOUR = _hourly_limiter(20)
# Per key and not per client, because the client identifier can be spoofed (see #195).
_REGISTER_PER_KEY_PER_HOUR = _hourly_limiter(20)


def _read_rate_limited(request: Request) -> bool:
    """Whether this read request is over the limit."""
    return not _READ_LIMITER.is_allowed(IPRateLimiter.get_client_identifier(request))


def _register_rate_limited(request: Request, key: str) -> bool:
    """Whether this registration is over one of the three limits: per minute, hour, or key."""
    client_id = IPRateLimiter.get_client_identifier(request)
    return not (
        _REGISTER_PER_MINUTE.is_allowed(client_id)
        and _REGISTER_PER_HOUR.is_allowed(client_id)
        and _REGISTER_PER_KEY_PER_HOUR.is_allowed(fingerprint_for_log(key))
    )


def _too_many_requests(request: Request) -> Response:
    """The invite error page as a 429.

    The language is nl, as on the "invite not found" path: the limit is judged before the
    project is looked up, so there is no project language to read.
    """
    return render(
        request,
        template="bg/invite-error.html.j2",
        context={
            "request": request,
            "error_title": "Te veel verzoeken",
            "error_message": "Je hebt te veel verzoeken gedaan. Wacht even en probeer het opnieuw.",
            "language": "nl",
        },
        status_code=429,
    )


def _generate_pkce_pair() -> tuple[str, str]:
    """A PKCE pair: the code_verifier and the S256 code_challenge over it."""
    code_verifier = secrets.token_urlsafe(32)

    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    return code_verifier, code_challenge


def _get_keycloak_url_for_cluster(cluster: str) -> str:
    # Imported here to avoid a circular import.
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
    """The Keycloak authorization URL for a project realm.

    With ``idp_hint`` the login screen is skipped and Keycloak goes straight to that IDP.
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
    """Exchange an authorization code for tokens; ``redirect_uri`` must match the original.

    Raises ``httpx.HTTPError`` when the exchange fails or Keycloak cannot be reached.
    """
    token_endpoint = f"{keycloak_url}/realms/{realm_name}/protocol/openid-connect/token"

    data = {
        "grant_type": "authorization_code",
        "client_id": settings.INVITE_CLIENT_ID,
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }

    async with httpx.AsyncClient(timeout=KEYCLOAK_HTTP_TIMEOUT_SECONDS) as client:
        response = await client.post(
            token_endpoint,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        return response.json()


async def _get_userinfo(keycloak_url: str, realm_name: str, access_token: str) -> dict[str, Any]:
    """The user info Keycloak hands back for this access token.

    Raises ``httpx.HTTPError`` when the request fails or Keycloak cannot be reached.
    """
    userinfo_endpoint = f"{keycloak_url}/realms/{realm_name}/protocol/openid-connect/userinfo"

    async with httpx.AsyncClient(timeout=KEYCLOAK_HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(
            userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        return response.json()


invite_router = APIRouter(prefix="/invite", tags=["invites"])


async def _find_project_by_invite_key(key: str) -> tuple[str, dict[str, Any], dict[str, Any], str] | None:
    """The project, its data, the invite and its cluster, or None when no project holds the key.

    Reads the projects the store has in memory; it does not refresh them first.
    """
    handler = ProjectFileHandler()

    all_projects = get_project_store().get_all()

    for project in all_projects:
        project_name = project.name
        project_data = project.data
        if not project_data:
            continue

        invite = handler.get_invite_by_key(project_data, key)
        if invite:
            deployments: list[Any] = project_data.get("deployments", [])
            cluster: str = "local"
            if deployments and len(deployments) > 0:
                first_deployment: Any = deployments[0]
                if isinstance(first_deployment, dict):
                    cluster_value: Any = first_deployment.get("cluster", "local")
                    cluster = str(cluster_value) if cluster_value else "local"

            logger.debug(
                f"Found invite key {fingerprint_for_log(key)} in project '{project_name}' (cluster: {cluster})"
            )
            return project_name, project_data, invite, cluster

    logger.debug(f"Invite key {fingerprint_for_log(key)} not found in any project")
    return None


def _get_language(request: Request, project_data: dict[str, Any]) -> str:
    """The two-letter language code the invite page renders in."""
    invite_manager = InviteManager()
    settings_dict = invite_manager.project_file_handler.get_invite_settings(project_data)
    default_lang = settings_dict.get("default_language", "nl")

    return invite_manager.detect_language(
        request.query_params.get("lang"),
        request.headers.get("accept-language"),
        default_lang,
    )


def _get_error_messages(language: str) -> dict[str, str]:
    """Per error code the message, in this language."""
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
    ``UserExistsError``, so the flow has to show an error page instead of success.
    """
    errors = assigned.get("errors") if isinstance(assigned, dict) else None
    if not isinstance(errors, list):
        return False
    return any(isinstance(msg, str) and msg.startswith("Realm roles not found") for msg in errors)


@invite_router.get("/{key}", response_class=HTMLResponse)
async def invite_landing(request: Request, key: str) -> Response:
    """The invite landing page, with the authentication methods the realm actually offers."""
    if _read_rate_limited(request):
        return _too_many_requests(request)

    result = await _find_project_by_invite_key(key)
    if not result:
        return render(
            request,
            template="bg/invite-error.html.j2",
            context={
                "request": request,
                "error_title": "Uitnodiging niet gevonden",
                "error_message": "De opgegeven uitnodiging bestaat niet of is niet meer geldig.",
                "language": "nl",
            },
        )

    project_name, project_data, invite, cluster = result
    language = _get_language(request, project_data)

    invite_manager = InviteManager()

    realm_name = generate_project_realm_name(project_name, cluster)
    realm_auth = await invite_manager.get_realm_auth_methods(realm_name)

    # An invite can restrict what the realm offers, never expand it, so a method has to be
    # available in the realm AND allowed by the invite.
    invite_auth_config = invite_manager.project_file_handler.get_invite_auth_methods(project_data, invite)

    allow_sso = realm_auth["sso"] and invite_auth_config["sso"]
    allow_local = realm_auth["local"] and invite_auth_config["local"]

    message = invite_manager.project_file_handler.get_invite_message(invite, language)
    display_name = invite_manager.project_file_handler.get_invite_title(invite, project_name, project_data, language)
    contact_email = invite.get("contact_email", "")

    identity_providers = realm_auth.get("identity_providers", [])

    return render(
        request,
        template="bg/invite-landing.html.j2",
        context={
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
    """Start the SSO flow of an invite, at the realm of the PROJECT and with PKCE.

    The client is public, so PKCE is what binds the callback to this browser.
    """
    if _read_rate_limited(request):
        return _too_many_requests(request)

    result = await _find_project_by_invite_key(key)
    if not result:
        raise HTTPException(status_code=404, detail="Invite not found")

    project_name, project_data, invite, cluster = result
    invite_manager = InviteManager()

    try:
        invite_manager.validate_auth_method(project_data, invite, "sso")
    except InviteError as e:
        raise HTTPException(status_code=400, detail=e.message) from e

    code_verifier, code_challenge = _generate_pkce_pair()
    # Against CSRF: the callback has to hand this value back.
    state = secrets.token_urlsafe(16)

    realm_name = generate_project_realm_name(project_name, cluster)
    keycloak_url = _get_keycloak_url_for_cluster(cluster)
    callback_url = str(request.url_for("invite_sso_callback", key=key))

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

    auth_url = _build_realm_auth_url(
        keycloak_url=keycloak_url,
        realm_name=realm_name,
        redirect_uri=callback_url,
        state=state,
        code_challenge=code_challenge,
    )

    # The callback URL carries the key in its path, so it goes through the redaction too.
    logger.info(
        f"Starting invite SSO flow for key {fingerprint_for_log(key)}, realm: {realm_name}, "
        f"callback: {redact_invite_key_in_path(callback_url)}"
    )

    return RedirectResponse(url=auth_url, status_code=302)


@invite_router.get("/{key}/idp/{idp_alias}")
async def invite_idp_start(request: Request, key: str, idp_alias: str) -> Response:
    """Start the SSO flow straight at one identity provider, skipping the Keycloak login screen.

    ``idp_alias`` is a Keycloak identity provider alias such as 'sso-rijk', and it has to be
    one the realm actually holds.
    """
    if _read_rate_limited(request):
        return _too_many_requests(request)

    result = await _find_project_by_invite_key(key)
    if not result:
        raise HTTPException(status_code=404, detail="Invite not found")

    project_name, project_data, invite, cluster = result
    invite_manager = InviteManager()

    try:
        invite_manager.validate_auth_method(project_data, invite, "sso")
    except InviteError as e:
        raise HTTPException(status_code=400, detail=e.message) from e

    realm_name = generate_project_realm_name(project_name, cluster)
    realm_auth = await invite_manager.get_realm_auth_methods(realm_name)
    identity_providers = realm_auth.get("identity_providers", [])

    valid_aliases = [idp["alias"] for idp in identity_providers]
    if idp_alias not in valid_aliases:
        logger.warning(f"Invalid IDP alias '{idp_alias}' for invite {fingerprint_for_log(key)}. Valid: {valid_aliases}")
        raise HTTPException(status_code=404, detail=f"Identity provider '{idp_alias}' not found")

    code_verifier, code_challenge = _generate_pkce_pair()
    # Against CSRF: the callback has to hand this value back.
    state = secrets.token_urlsafe(16)

    keycloak_url = _get_keycloak_url_for_cluster(cluster)
    # The same callback as the plain SSO flow.
    callback_url = str(request.url_for("invite_sso_callback", key=key))

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

    auth_url = _build_realm_auth_url(
        keycloak_url=keycloak_url,
        realm_name=realm_name,
        redirect_uri=callback_url,
        state=state,
        code_challenge=code_challenge,
        idp_hint=idp_alias,
    )

    logger.info(f"Starting invite IDP flow for key {fingerprint_for_log(key)}, realm: {realm_name}, idp: {idp_alias}")

    return RedirectResponse(url=auth_url, status_code=302)


@invite_router.get("/{key}/sso/callback")
async def invite_sso_callback(request: Request, key: str) -> Response:
    """Exchange the authorization code at the project realm, then assign the invite's rights.

    Redirects to the success page, or to the error page with a code.
    """
    invite_flow = request.session.get("invite_flow")
    if not invite_flow or invite_flow.get("key") != key:
        logger.warning(f"SSO callback session mismatch for key {fingerprint_for_log(key)}")
        return RedirectResponse(url=f"/invite/{key}/error?code=session_error", status_code=302)

    state_param = request.query_params.get("state")
    if state_param != invite_flow.get("state"):
        logger.warning(f"SSO callback state mismatch for key {fingerprint_for_log(key)}")
        return RedirectResponse(url=f"/invite/{key}/error?code=session_error", status_code=302)

    error = request.query_params.get("error")
    if error:
        error_description = request.query_params.get("error_description", error)
        logger.warning(f"SSO callback error for key {fingerprint_for_log(key)}: {error} - {error_description}")
        return RedirectResponse(url=f"/invite/{key}/error?code=sso_error", status_code=302)

    code = request.query_params.get("code")
    if not code:
        logger.warning(f"SSO callback missing code for key {fingerprint_for_log(key)}")
        return RedirectResponse(url=f"/invite/{key}/error?code=missing_code", status_code=302)

    result = await _find_project_by_invite_key(key)
    if not result:
        return RedirectResponse(url=f"/invite/{key}/error?code=invite_not_found", status_code=302)

    project_name, project_data, invite, cluster = result

    try:
        keycloak_url = invite_flow["keycloak_url"]
        realm_name = invite_flow["realm_name"]
        code_verifier = invite_flow["code_verifier"]
        redirect_uri = invite_flow["redirect_uri"]

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

        user_info = await _get_userinfo(keycloak_url, realm_name, access_token)

        if not user_info or not user_info.get("email"):
            raise InviteError("Failed to retrieve user information", "no_user_info")

        logger.info(f"SSO callback for invite {fingerprint_for_log(key)}, user: {user_info.get('email', 'unknown')}")

        # The user is authenticated in the project realm already; what is left is assigning
        # the roles and groups of the invite.
        invite_manager = InviteManager()

        result_data = await invite_manager.complete_sso_invite(
            project_data=project_data,
            project_name=project_name,
            invite=invite,
            user_info=user_info,
            realm_name=realm_name,
        )

        request.session.pop("invite_flow", None)

        # The account was created but a named realm role could not be granted: an error page
        # and not success, because retrying loops on UserExistsError.
        if _realm_roles_unassigned(result_data["assigned"]):
            return RedirectResponse(url=f"/invite/{key}/error?code=role_not_assigned", status_code=302)

        request.session["invite_success"] = {
            "email": result_data["email"],
            "created": result_data["created"],
            "assigned": result_data["assigned"],
        }

        return RedirectResponse(url=f"/invite/{key}/success", status_code=302)

    except httpx.HTTPError as e:
        logger.exception(f"HTTP error in SSO callback for invite {fingerprint_for_log(key)}: {e}")
        return RedirectResponse(url=f"/invite/{key}/error?code=token_error", status_code=302)
    except InviteDomainError as e:
        error_url = f"/invite/{key}/error?code=domain_mismatch&domain={e.required_domain}"
        return RedirectResponse(url=error_url, status_code=302)
    except InviteError as e:
        return RedirectResponse(url=f"/invite/{key}/error?code={e.error_code}", status_code=302)
    except Exception:
        logger.exception(f"Error in SSO callback for invite {fingerprint_for_log(key)}")
        return RedirectResponse(url=f"/invite/{key}/error?code=generic_error", status_code=302)


@invite_router.get("/{key}/register", response_class=HTMLResponse)
async def invite_register_form(request: Request, key: str) -> Response:
    """The form for creating a local account on this invite."""
    if _read_rate_limited(request):
        return _too_many_requests(request)

    result = await _find_project_by_invite_key(key)
    if not result:
        return render(
            request,
            template="bg/invite-error.html.j2",
            context={
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
        return render(
            request,
            template="bg/invite-error.html.j2",
            context={
                "request": request,
                "error_title": error_messages["auth_method_not_allowed"],
                "error_message": auth_error_msg,
                "language": language,
            },
        )

    display_name = invite_manager.project_file_handler.get_invite_title(invite, project_name, project_data, language)
    message = invite_manager.project_file_handler.get_invite_message(invite, language)
    domain_restriction = invite.get("restrict_domain")

    return render(
        request,
        template="bg/invite-register.html.j2",
        context={
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
    """Create the local account the form asks for, or render the form again with its errors."""
    if _register_rate_limited(request, key):
        return _too_many_requests(request)

    result = await _find_project_by_invite_key(key)
    if not result:
        return render(
            request,
            template="bg/invite-error.html.j2",
            context={
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
        return render(
            request,
            template="bg/invite-error.html.j2",
            context={
                "request": request,
                "error_title": error_messages.get(e.error_code, error_messages["generic_error"]),
                "error_message": e.message,
                "language": language,
            },
        )

    form = await request.form()
    form_data = {
        "email": str(form.get("email", "")).strip(),
        "first_name": str(form.get("first_name", "")).strip(),
        "last_name": str(form.get("last_name", "")).strip(),
        "password": str(form.get("password", "")),
    }
    password_confirm = str(form.get("password_confirm", ""))

    # Every field is judged, so the form comes back with all of its errors at once.
    errors: dict[str, str] = {}

    if not form_data["email"]:
        errors["email"] = error_messages["missing_email"]
    else:
        email_pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
        if not re.match(email_pattern, form_data["email"]):
            errors["email"] = error_messages["invalid_email"]
        else:
            restrict_domain = invite.get("restrict_domain")
            if restrict_domain:
                domain = restrict_domain if restrict_domain.startswith("@") else f"@{restrict_domain}"
                if not form_data["email"].lower().endswith(domain.lower()):
                    errors["email"] = error_messages["domain_mismatch"]

    if not form_data["first_name"]:
        errors["first_name"] = error_messages["missing_first_name"]
    if not form_data["last_name"]:
        errors["last_name"] = error_messages["missing_last_name"]

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

    if (form_data["password"] and form_data["password"] != password_confirm) or not password_confirm:
        errors["password_confirm"] = error_messages["password_mismatch"]

    if errors:
        display_name = invite_manager.project_file_handler.get_invite_title(
            invite, project_name, project_data, language
        )
        message = invite_manager.project_file_handler.get_invite_message(invite, language)
        return render(
            request,
            template="bg/invite-register.html.j2",
            context={
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

        # The account was created but a named realm role could not be granted: an error page
        # and not success, because retrying loops on UserExistsError.
        if _realm_roles_unassigned(result_data["assigned"]):
            return RedirectResponse(url=f"/invite/{key}/error?code=role_not_assigned", status_code=302)

        # ``verify_email`` stands on this branch only: through ``trustEmail`` the SSO road
        # hands over an already verified user and may not show the text about confirming. When
        # that road lands on an account this one left unconfirmed, the page says nothing about
        # it; see docs/KNOWN-ISSUES.md.
        request.session["invite_success"] = {
            "email": result_data["email"],
            "created": result_data["created"],
            "assigned": result_data["assigned"],
            "verify_email": True,
            "verification_mail_sent": result_data["verification_mail_sent"],
        }

        return RedirectResponse(url=f"/invite/{key}/success", status_code=302)

    except (InviteDomainError, UserExistsError, InviteError) as e:
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

        general_error = None
        if e.error_code == "user_exists":
            general_error = error_messages.get(e.error_code, e.message)

        display_name = invite_manager.project_file_handler.get_invite_title(
            invite, project_name, project_data, language
        )
        return render(
            request,
            template="bg/invite-register.html.j2",
            context={
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
        logger.exception(f"Error creating local account for invite {fingerprint_for_log(key)}")
        display_name = invite_manager.project_file_handler.get_invite_title(
            invite, project_name, project_data, language
        )
        return render(
            request,
            template="bg/invite-register.html.j2",
            context={
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
    """The page that closes the invite flow."""
    if _read_rate_limited(request):
        return _too_many_requests(request)

    result = await _find_project_by_invite_key(key)
    if not result:
        return render(
            request,
            template="bg/invite-error.html.j2",
            context={
                "request": request,
                "error_title": "Uitnodiging niet gevonden",
                "error_message": "De opgegeven uitnodiging bestaat niet of is niet meer geldig.",
                "language": "nl",
            },
        )

    project_name, project_data, invite, _cluster = result
    language = _get_language(request, project_data)

    # Nothing in the session means this page was opened without walking the flow.
    success_info = request.session.pop("invite_success", {})
    if not success_info:
        return RedirectResponse(url=f"/invite/{key}", status_code=302)

    invite_manager = InviteManager()

    success_title = invite_manager.project_file_handler.get_invite_success_title(invite, language)
    success_button = invite_manager.project_file_handler.get_invite_success_button(invite, language)
    application_url = resolve_invite_url(invite, project_data)
    display_name = invite_manager.project_file_handler.get_invite_title(invite, project_name, project_data, language)

    return render(
        request,
        template="bg/invite-success.html.j2",
        context={
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
            "verify_email": success_info.get("verify_email", False),
            "verification_mail_sent": success_info.get("verification_mail_sent", False),
            "contact_email": invite.get("contact_email", ""),
        },
    )


@invite_router.get("/{key}/error", response_class=HTMLResponse)
async def invite_error(request: Request, key: str) -> Response:
    """The error page of the invite flow, named by the ``code`` query parameter."""
    if _read_rate_limited(request):
        return _too_many_requests(request)

    # Looked up for the language only, so a key that no project holds still renders a page.
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

    if error_code == "domain_mismatch" and domain:
        if language == "nl":
            error_message = f"Je e-mailadres moet eindigen op '{domain}'."
        else:
            error_message = f"Your email address must end with '{domain}'."
    elif error_code == "user_exists":
        if language == "nl":
            error_message = "Er bestaat al een account met dit e-mailadres. Gebruik de SSO login optie."
        else:
            error_message = "An account with this email already exists. Use the SSO login option."
    elif error_code == "role_not_assigned":
        # The account WAS created; only the intended role could not be granted. The text says
        # so, warns that retrying will not work (it hits UserExistsError), and points at the
        # contact address of the invite.
        contact = invite.get("contact_email")
        if language == "nl":
            error_message = (
                "Je account is aangemaakt, maar de bijbehorende rol kon niet worden toegekend. "
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

    return render(
        request,
        template="bg/invite-error.html.j2",
        context={
            "request": request,
            "error_title": error_title,
            "error_message": error_message,
            "language": language,
            "invite_key": key,
        },
    )
