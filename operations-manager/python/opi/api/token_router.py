"""The members of a project and their tokens: read, issue, rotate and revoke.

Everything hangs under ``/api/v2/projects/{p}/leden``, because a token belongs to a member and
a member to a project. There is no path for "my tokens across all projects": such a list would
name projects to someone who may just have been taken out of them.

A caller legitimates with an SSO bearer token and NOT with a project key or an API token:
issuing a token asks for an authenticated person, so the caller cannot pick who they are. The
same rule stands as :data:`HUMAN_REQUIRED` at the enforcement point, which keeps a service
account out along the ordinary API as well.

The issued value stands ONCE, in the answer to the creation. It cannot be read back anywhere
after that; whoever loses it rotates the token.

Every writing handling here asks for a fresh second factor on top of that: issuing, rotating or
revoking a token and changing a role all hand out or take away a credential. The caller
activates one with ``POST /api/v2/step-up``, measured against the owner of the token;
``opi.http.auth.otp_step_up`` describes the refusal. The read route asks for none, it changes
nothing.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from opi.api.params import ProjectNamePath
from opi.core.authz.actions import Action, action
from opi.http.auth.auth_decorators import get_current_user, requires_step_up
from opi.http.auth.step_up_conditions import vraagt_altijd_step_up
from opi.http.auth.user_token_auth import validate_user_token
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
from opi.services.project_authorization import is_user_authorized_for_project
from opi.services.step_up import StepUpAction

logger = logging.getLogger(__name__)

token_router: APIRouter = APIRouter(prefix="/api/v2", tags=["tokens"], default_response_class=JSONResponse)


class ServiceAccountAanvraag(BaseModel):
    """What adding a service account to the members of a project asks for."""

    naam: str = Field(description="DNS-label; hieronder staat het account in het logboek en in de git-trailer")
    roles: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ROLES),
        description="Dezelfde rollen als een mens. Wat elke rol opent staat op /rollen",
        examples=[["developer-ci"]],
    )


class RolAanvraag(BaseModel):
    """What changing the roles of a member asks for."""

    roles: list[str] = Field(description="De volledige nieuwe lijst; een lege lijst wordt geweigerd")


class TokenAanvraag(BaseModel):
    """What creating a token asks for. No scopes: what a token may do follows from the role."""

    naam: str = Field(description="Korte naam van het token, uniek binnen dit lid")
    dagen: int = Field(gt=0, description="Hoeveel dagen het token geldig is; het platform kent een maximum")


class UitgegevenToken(BaseModel):
    """The answer to a creation. ``token`` stands here once and nowhere else."""

    token: str
    lid: str
    naam: str


def _email(request: Request) -> str:
    user = get_current_user(request) or {}
    email = str(user.get("email", ""))
    if not email:
        raise HTTPException(status_code=401, detail="Dit token draagt geen e-mailadres")
    return email


def _require_membership(project_name: str, email: str) -> str:
    """Refuse whoever is not a member of this project, and hand back their address.

    The same boundary as the Team tab, which shows this list to a member only
    (``render_project_page``). Issuing and revoking have their own role rule in
    ``api_token_service``; reading has none, and without this check every platform token
    could read off the members of every project.
    """
    if not is_user_authorized_for_project(project_name, email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")
    return email


def _refused(exc: TokenRequestRefusedError) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


@token_router.get("/projects/{project_name}/leden", summary="De leden van dit project en hun tokens")
@action(Action.TOKEN_READ)
@validate_user_token
async def project_leden(request: Request, project_name: ProjectNamePath) -> JSONResponse:
    """The people and the service accounts of this project, with their role and their tokens.

    Without the hashes: whoever holds a hash can measure a guessed token offline.
    """
    _require_membership(project_name, _email(request))
    return JSONResponse(content={"leden": _as_json(members_overview(project_name))})


def _as_json(members: list[dict]) -> list[dict]:
    """Expiry dates as ISO text, because JSONResponse encodes no datetime."""
    return [
        {**member, "tokens": [{**t, "vervalt": t["vervalt"].isoformat()} for t in member["tokens"]]}
        for member in members
    ]


@token_router.post("/projects/{project_name}/leden", status_code=201, summary="Voeg een service account toe")
@action(Action.MEMBER_MANAGE)
@validate_user_token
@requires_step_up(vraagt_altijd_step_up, action=StepUpAction.SERVICE_ACCOUNT_ADD)
async def voeg_lid_toe(
    request: Request, project_name: ProjectNamePath, aanvraag: ServiceAccountAanvraag = Body(...)
) -> JSONResponse:
    """Put a service account in the members of this project. Only an admin may do that.

    Adding a person does not go along here but along the Team tab: that carries an invitation
    and a Keycloak user, which is exactly what a service account has not.
    """
    email = _require_membership(project_name, _email(request))
    try:
        await add_service_account(project_name=project_name, name=aanvraag.naam, roles=aanvraag.roles, by=email)
    except TokenRequestRefusedError as exc:
        raise _refused(exc) from exc
    return JSONResponse(content={"status": "toegevoegd", "naam": aanvraag.naam}, status_code=201)


@token_router.delete("/projects/{project_name}/leden/{naam}", summary="Verwijder een service account")
@action(Action.MEMBER_MANAGE)
@validate_user_token
@requires_step_up(vraagt_altijd_step_up, action=StepUpAction.SERVICE_ACCOUNT_REMOVE)
async def verwijder_lid(request: Request, project_name: ProjectNamePath, naam: str) -> JSONResponse:
    """Take a service account out of the members. Its tokens go in the same commit."""
    email = _require_membership(project_name, _email(request))
    try:
        await remove_service_account(project_name=project_name, name=naam, by=email)
    except TokenRequestRefusedError as exc:
        raise _refused(exc) from exc
    return JSONResponse(content={"status": "verwijderd", "naam": naam})


@token_router.put("/projects/{project_name}/leden/{naam}/rol", summary="Wijzig de rollen van een lid")
@action(Action.MEMBER_MANAGE)
@validate_user_token
@requires_step_up(vraagt_altijd_step_up, action=StepUpAction.ROLE_CHANGE)
async def zet_rol(
    request: Request, project_name: ProjectNamePath, naam: str, aanvraag: RolAanvraag = Body(...)
) -> JSONResponse:
    """Give a member other roles. Its existing tokens follow those roles at once."""
    email = _require_membership(project_name, _email(request))
    try:
        await set_member_roles(project_name=project_name, name=naam, roles=aanvraag.roles, by=email)
    except TokenRequestRefusedError as exc:
        raise _refused(exc) from exc
    return JSONResponse(content={"status": "gewijzigd", "naam": naam, "roles": aanvraag.roles})


@token_router.post("/projects/{project_name}/leden/{naam}/tokens", status_code=201, summary="Geef een token uit")
@action(Action.TOKEN_MANAGE)
@validate_user_token
@requires_step_up(vraagt_altijd_step_up, action=StepUpAction.TOKEN_ISSUE)
async def maak_token(
    request: Request, project_name: ProjectNamePath, naam: str, aanvraag: TokenAanvraag = Body(...)
) -> JSONResponse:
    """Issue a token to this member. The value stands once, in this answer.

    An admin does that for a service account; a person does it for themselves.
    """
    email = _require_membership(project_name, _email(request))
    try:
        value = await issue_token(
            project_name=project_name, name=naam, token_name=aanvraag.naam, days=aanvraag.dagen, by=email
        )
    except TokenRequestRefusedError as exc:
        raise _refused(exc) from exc
    return JSONResponse(
        content=UitgegevenToken(token=value, lid=naam, naam=aanvraag.naam).model_dump(), status_code=201
    )


@token_router.post("/projects/{project_name}/leden/{naam}/tokens/{tokennaam}/:roteer", summary="Roteer een token")
@action(Action.TOKEN_MANAGE)
@validate_user_token
@requires_step_up(vraagt_altijd_step_up, action=StepUpAction.TOKEN_ROTATE)
async def roteer(
    request: Request, project_name: ProjectNamePath, naam: str, tokennaam: str, aanvraag: TokenAanvraag = Body(...)
) -> JSONResponse:
    """Revoke this token and issue a new one under the same name."""
    email = _require_membership(project_name, _email(request))
    try:
        value = await rotate_token(
            project_name=project_name, name=naam, token_name=tokennaam, days=aanvraag.dagen, by=email
        )
    except TokenRequestRefusedError as exc:
        raise _refused(exc) from exc
    return JSONResponse(content=UitgegevenToken(token=value, lid=naam, naam=tokennaam).model_dump())


@token_router.delete("/projects/{project_name}/leden/{naam}/tokens/{tokennaam}", summary="Trek een token in")
@action(Action.TOKEN_MANAGE)
@validate_user_token
@requires_step_up(vraagt_altijd_step_up, action=StepUpAction.TOKEN_REVOKE)
async def trek_in(request: Request, project_name: ProjectNamePath, naam: str, tokennaam: str) -> JSONResponse:
    """Revoke a token. That is a commit, so it stands in the history of the project."""
    email = _require_membership(project_name, _email(request))
    try:
        await revoke_token(project_name=project_name, name=naam, token_name=tokennaam, by=email)
    except TokenRequestRefusedError as exc:
        raise _refused(exc) from exc
    return JSONResponse(content={"status": "ingetrokken", "lid": naam, "naam": tokennaam})
