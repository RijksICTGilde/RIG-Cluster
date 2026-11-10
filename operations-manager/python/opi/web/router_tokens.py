"""Managing members and tokens from the portal.

The rules live in :mod:`opi.services.api_token_service` and not here, so the form and the API
run the same logic path.

The issued value comes back ONCE and is thrown away as it is shown. It stays server-side while
it waits: Starlette's cookie session is signed and not encrypted, so a token in the session
would sit base64-readable on the browser's disk until the next GET. ``router_otp`` makes the
same choice and puts a fingerprint in the session rather than the secret.

Every action here asks for a fresh step-up, with the window from ``opi.services.step_up``. The
dialog takes the place of the form, so the fields of the action travel along in the repeat:
``fields`` on the decorator lists them per route.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from opi.core.authz.actions import Action, action
from opi.core.authz.engine import ROLE_DESCRIPTIONS, Role, role_label
from opi.core.config import settings
from opi.http.auth.auth_decorators import get_current_user, requires_sso, requires_step_up
from opi.http.render.lotc_switch import project_tab_url
from opi.services.api_token_service import (
    DEFAULT_ROLES,
    TokenRequestRefusedError,
    add_service_account,
    issue_token,
    members_overview,
    remove_service_account,
    revoke_token,
    rotate_token,
    set_member_roles,
)
from opi.services.members import MemberKind
from opi.services.step_up import StepUpAction

logger = logging.getLogger(__name__)

token_web_router = APIRouter(prefix="/projects", tags=["tokens-web"])

#: Where the reference to the new token and a refusal sit between the POST and the next GET.
NEW_TOKEN_SESSION_KEY = "nieuw_token"  # noqa: S105  # a session key, not a secret
TOKEN_ERROR_SESSION_KEY = "tokenfout"  # noqa: S105  # a session key, not a secret

#: How long an issued value waits for its one-time display. The normal path is the 303 that
#: goes straight there; whoever does not open the tab lets it expire here.
_DISPLAY_SECONDS = 300

#: The issued values still waiting for their one-time display, per reference. OPI runs as a
#: single replica, so this does not have to be shared; a restart between the POST and the GET
#: costs the display, and then the token can be rotated.
_pending: dict[str, tuple[float, dict[str, str]]] = {}


def _hold_for_display(details: dict[str, str]) -> str:
    """Keep an issued value server-side and return the reference to it."""
    now = time.monotonic()
    for reference, (expires_at, _) in list(_pending.items()):
        if expires_at <= now:
            del _pending[reference]
    reference = secrets.token_urlsafe(16)
    _pending[reference] = (now + _DISPLAY_SECONDS, details)
    return reference


def _take_once(reference: str | None) -> dict[str, str] | None:
    """The value behind this reference, once; after that it is gone."""
    if not reference:
        return None
    held = _pending.pop(reference, None)
    if held is None:
        return None
    expires_at, details = held
    return details if expires_at > time.monotonic() else None


def _email(request: Request) -> str:
    user = get_current_user(request) or {}
    email = str(user.get("email", "")).lower()
    if not email:
        raise HTTPException(status_code=403, detail="Geen aangemelde gebruiker")
    return email


def _back_to_team(request: Request, project_name: str) -> Response:
    """Back to the Team tab, as a full navigation under htmx.

    The forms post with htmx because the step-up puts a dialog in the place of the form. A 303
    would leave htmx hanging the whole page in that dialog, so the refresh goes as HX-Redirect.
    """
    url = project_tab_url(project_name, "team")
    if request.headers.get("HX-Request"):
        return HTMLResponse(content="", headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


async def _form_fields(request: Request) -> dict[str, str]:
    """The form as a flat dict; a multi-select becomes a comma-separated list.

    The same shape ``fields_from_form`` in audit_trail returns for the step-up repeat, so a
    route need not know whether it is reading the form or the repeat.
    """
    form = await request.form()
    getlist = getattr(form, "getlist", None)
    if getlist is None:
        # A test that supplies a plain dict as the form.
        return {key: str(value) for key, value in form.items()}
    return {key: ",".join(str(value) for value in getlist(key)) for key in form}


def _roles_from(fields: dict[str, str]) -> list[str]:
    """The roles chosen in the form.

    The multi-select sends ``roles`` several times; ``fields_from_form`` has already joined
    those into a comma-separated list, and the step-up repeat sends that same shape back.
    """
    return [part.strip() for part in fields.get("roles", "").split(",") if part.strip()]


def _days(fields: dict[str, str]) -> int:
    try:
        return int(fields.get("dagen") or "0")
    except ValueError as exc:
        raise TokenRequestRefusedError("Vul een aantal dagen in.") from exc


@token_web_router.post("/{project_name}/leden/toevoegen")
@action(Action.MEMBER_MANAGE)
@requires_sso
@requires_step_up(action=StepUpAction.SERVICE_ACCOUNT_ADD, fields=("naam", "roles"))
async def voeg_service_account_toe_web(request: Request, project_name: str) -> Response:
    """Put a service account in the members of this project."""
    fields = await _form_fields(request)
    try:
        await add_service_account(
            project_name=project_name,
            name=fields.get("naam", ""),
            roles=_roles_from(fields) or list(DEFAULT_ROLES),
            by=_email(request),
        )
    except TokenRequestRefusedError as exc:
        request.session[TOKEN_ERROR_SESSION_KEY] = str(exc)
    return _back_to_team(request, project_name)


@token_web_router.post("/{project_name}/leden/{naam}/verwijderen")
@action(Action.MEMBER_MANAGE)
@requires_sso
@requires_step_up(action=StepUpAction.SERVICE_ACCOUNT_REMOVE)
async def verwijder_service_account_web(request: Request, project_name: str, naam: str) -> Response:
    """Take a service account out of the members; its tokens go in the same commit."""
    try:
        await remove_service_account(project_name=project_name, name=naam, by=_email(request))
    except TokenRequestRefusedError as exc:
        request.session[TOKEN_ERROR_SESSION_KEY] = str(exc)
    return _back_to_team(request, project_name)


@token_web_router.post("/{project_name}/leden/{naam}/rol")
@action(Action.MEMBER_MANAGE)
@requires_sso
@requires_step_up(action=StepUpAction.ROLE_CHANGE, fields=("roles",))
async def wijzig_rol_web(request: Request, project_name: str, naam: str) -> Response:
    """Give a member other roles; its existing tokens follow those roles at once."""
    fields = await _form_fields(request)
    try:
        await set_member_roles(project_name=project_name, name=naam, roles=_roles_from(fields), by=_email(request))
    except TokenRequestRefusedError as exc:
        request.session[TOKEN_ERROR_SESSION_KEY] = str(exc)
    return _back_to_team(request, project_name)


@token_web_router.post("/{project_name}/leden/{naam}/tokens")
@action(Action.TOKEN_MANAGE)
@requires_sso
@requires_step_up(action=StepUpAction.TOKEN_ISSUE, fields=("tokennaam", "dagen"))
async def maak_token_web(request: Request, project_name: str, naam: str) -> Response:
    """Issue a token to this member; the value can be seen once afterwards."""
    fields = await _form_fields(request)
    try:
        value = await issue_token(
            project_name=project_name,
            name=naam,
            token_name=fields.get("tokennaam", ""),
            days=_days(fields),
            by=_email(request),
        )
    except TokenRequestRefusedError as exc:
        request.session[TOKEN_ERROR_SESSION_KEY] = str(exc)
        return _back_to_team(request, project_name)

    request.session[NEW_TOKEN_SESSION_KEY] = _hold_for_display({"token": value, "eigenaar": naam})
    return _back_to_team(request, project_name)


@token_web_router.post("/{project_name}/leden/{naam}/tokens/{tokennaam}/roteren")
@action(Action.TOKEN_MANAGE)
@requires_sso
@requires_step_up(action=StepUpAction.TOKEN_ROTATE, fields=("dagen",))
async def roteer_token_web(request: Request, project_name: str, naam: str, tokennaam: str) -> Response:
    """Revoke this token and issue a new one under the same name."""
    fields = await _form_fields(request)
    try:
        value = await rotate_token(
            project_name=project_name,
            name=naam,
            token_name=tokennaam,
            days=_days(fields),
            by=_email(request),
        )
    except TokenRequestRefusedError as exc:
        request.session[TOKEN_ERROR_SESSION_KEY] = str(exc)
        return _back_to_team(request, project_name)

    request.session[NEW_TOKEN_SESSION_KEY] = _hold_for_display({"token": value, "eigenaar": naam})
    return _back_to_team(request, project_name)


@token_web_router.post("/{project_name}/leden/{naam}/tokens/{tokennaam}/intrekken")
@action(Action.TOKEN_MANAGE)
@requires_sso
@requires_step_up(action=StepUpAction.TOKEN_REVOKE)
async def trek_token_in_web(request: Request, project_name: str, naam: str, tokennaam: str) -> Response:
    """Revoke a token. That is a commit, so it stands in the history of the project."""
    try:
        await revoke_token(project_name=project_name, name=naam, token_name=tokennaam, by=_email(request))
    except TokenRequestRefusedError as exc:
        request.session[TOKEN_ERROR_SESSION_KEY] = str(exc)
    return _back_to_team(request, project_name)


def tokencontext(request: Request, project_name: str, user_email: str, is_admin: bool) -> dict[str, Any]:
    """What the Team tab has to show about members and tokens.

    Reads from the project file only, so no database is involved.
    """
    members = members_overview(project_name)
    for member in members:
        member["is_service_account"] = member["soort"] == MemberKind.SERVICE_ACCOUNT.value
        member["is_current_user"] = member["naam"].lower() == user_email
        # An admin manages the tokens of every member, a human only their own. Issuing for
        # another human is nobody's right: that would be a credential someone carries without
        # knowing it exists.
        member["mag_uitgeven"] = (member["is_service_account"] and is_admin) or member["is_current_user"]
        member["mag_intrekken"] = is_admin or member["is_current_user"]

    # The caller's own card goes first; the stable sort keeps the project file order for
    # the rest, and a caller without membership moves nothing.
    members.sort(key=lambda member: not member["is_current_user"])

    return {
        "leden": members,
        # Label and description from the register, so another name on screen is a change in one
        # place and not a migration of project files.
        "rollen": [
            {"sleutel": role.value, "label": role_label(role), "uitleg": ROLE_DESCRIPTIONS[role]} for role in Role
        ],
        "standaard_rollen": list(DEFAULT_ROLES),
        "is_project_beheerder": is_admin,
        "token_max_dagen_persoonlijk": settings.TOKEN_MAX_DAGEN_PERSOONLIJK,
        "token_max_dagen_service_account": settings.TOKEN_MAX_DAGEN_SERVICE_ACCOUNT,
        "nieuw_token": _take_once(request.session.pop(NEW_TOKEN_SESSION_KEY, None)),
        "tokenfout": request.session.pop(TOKEN_ERROR_SESSION_KEY, None),
    }
