"""De leden van een project en hun tokens: zien, uitgeven, roteren en intrekken.

Alles hangt onder ``/api/v2/projects/{p}/leden``, want een token hoort bij een lid en een
lid hoort bij een project. Er is geen pad voor "mijn tokens over alle projecten heen": een
token geldt voor een project, dus zo'n lijst zou projecten noemen aan iemand die daar
misschien net uit is.

Legitimatie gaat hier met een SSO-bearertoken en NIET met een projectsleutel of een
API-token: een token aanmaken vraagt een geauthenticeerde persoon, want de aanroeper hoort
niet zelf te kiezen wie hij is. Dezelfde regel staat als :data:`MENS_VERPLICHT` in het
handhavingspunt, zodat een service account er ook via de gewone API niet bij kan.

De uitgegeven waarde staat EEN keer in het antwoord op het aanmaken. Daarna is hij nergens
meer op te halen; wie hem kwijt is roteert het token.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse
from opi.api.params import ProjectNamePath
from opi.api.user_token_auth import validate_user_token
from opi.core.auth_decorators import get_current_user
from opi.core.handelingen import Handeling, handeling
from opi.services.api_token_service import (
    TokenAanvraagGeweigerd,
    geef_token_uit,
    leden_overzicht,
    roteer_token,
    trek_token_in,
    verwijder_service_account,
    voeg_service_account_toe,
    wijzig_rol,
)
from opi.services.project_authorization import is_user_authorized_for_project
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

token_router: APIRouter = APIRouter(prefix="/api/v2", tags=["tokens"], default_response_class=JSONResponse)


class ServiceAccountAanvraag(BaseModel):
    """Een service account toevoegen aan de leden van een project."""

    naam: str = Field(description="DNS-label; hieronder staat het account in het logboek en in de git-trailer")
    rol: str = Field(default="developer", description="Dezelfde rollen als een mens: admin, owner, member, developer")


class RolAanvraag(BaseModel):
    """De rol van een lid veranderen."""

    rol: str


class TokenAanvraag(BaseModel):
    """Wat er nodig is om een token te maken. Scopes staan er niet bij: wat een token mag
    volgt uit de rol van het lid."""

    naam: str = Field(description="Korte naam van het token, uniek binnen dit lid")
    dagen: int = Field(gt=0, description="Hoeveel dagen het token geldig is; het platform kent een maximum")


class UitgegevenToken(BaseModel):
    """Het antwoord op een aanmaak. ``token`` staat hier een keer en nergens anders."""

    token: str
    lid: str
    naam: str


def _email(request: Request) -> str:
    gebruiker = get_current_user(request) or {}
    email = str(gebruiker.get("email", ""))
    if not email:
        raise HTTPException(status_code=401, detail="Dit token draagt geen e-mailadres")
    return email


def _eis_lidmaatschap(project_name: str, email: str) -> str:
    """Weiger wie geen lid is van dit project, en geef zijn adres terug.

    Dezelfde grens als het tabblad Team, dat deze lijst ook alleen aan een lid toont
    (``render_project_page``). Uitgeven en intrekken hebben hun eigen rolregel in
    ``api_token_service``; lezen heeft die niet en zou er zonder deze controle op
    neerkomen dat elk platformtoken de leden van elk project kan oplezen.
    """
    if not is_user_authorized_for_project(project_name, email):
        raise HTTPException(status_code=403, detail="Geen toegang tot dit project")
    return email


def _geweigerd(exc: TokenAanvraagGeweigerd) -> HTTPException:
    return HTTPException(status_code=403, detail=str(exc))


@token_router.get("/projects/{project_name}/leden", summary="De leden van dit project en hun tokens")
@handeling(Handeling.TOKEN_LEZEN)
@validate_user_token
async def project_leden(request: Request, project_name: ProjectNamePath) -> JSONResponse:
    """De mensen en de service accounts van dit project, met hun rol en hun tokens.

    Zonder hashes: wie een hash heeft kan een geraden token offline toetsen.
    """
    _eis_lidmaatschap(project_name, _email(request))
    return JSONResponse(content={"leden": _serialiseerbaar(leden_overzicht(project_name))})


def _serialiseerbaar(leden: list[dict]) -> list[dict]:
    """Vervaldatums als ISO-tekst, want JSONResponse codeert geen datetime."""
    return [{**lid, "tokens": [{**t, "vervalt": t["vervalt"].isoformat()} for t in lid["tokens"]]} for lid in leden]


@token_router.post("/projects/{project_name}/leden", status_code=201, summary="Voeg een service account toe")
@handeling(Handeling.TOKEN_BEHEREN)
@validate_user_token
async def voeg_lid_toe(
    request: Request, project_name: ProjectNamePath, aanvraag: ServiceAccountAanvraag = Body(...)
) -> JSONResponse:
    """Zet een service account in de leden van dit project. Alleen een beheerder mag dat.

    Een mens toevoegen gaat niet langs hier maar langs het tabblad Team: daar hoort een
    uitnodiging en een Keycloak-gebruiker bij, en die heeft een service account juist niet.
    """
    email = _eis_lidmaatschap(project_name, _email(request))
    try:
        await voeg_service_account_toe(project_name=project_name, naam=aanvraag.naam, rol=aanvraag.rol, door=email)
    except TokenAanvraagGeweigerd as exc:
        raise _geweigerd(exc) from exc
    return JSONResponse(content={"status": "toegevoegd", "naam": aanvraag.naam}, status_code=201)


@token_router.delete("/projects/{project_name}/leden/{naam}", summary="Verwijder een service account")
@handeling(Handeling.TOKEN_BEHEREN)
@validate_user_token
async def verwijder_lid(request: Request, project_name: ProjectNamePath, naam: str) -> JSONResponse:
    """Haal een service account uit de leden. Zijn tokens gaan dezelfde commit mee uit."""
    email = _eis_lidmaatschap(project_name, _email(request))
    try:
        await verwijder_service_account(project_name=project_name, naam=naam, door=email)
    except TokenAanvraagGeweigerd as exc:
        raise _geweigerd(exc) from exc
    return JSONResponse(content={"status": "verwijderd", "naam": naam})


@token_router.put("/projects/{project_name}/leden/{naam}/rol", summary="Wijzig de rol van een lid")
@handeling(Handeling.TOKEN_BEHEREN)
@validate_user_token
async def zet_rol(
    request: Request, project_name: ProjectNamePath, naam: str, aanvraag: RolAanvraag = Body(...)
) -> JSONResponse:
    """Geef een lid een andere rol. Zijn bestaande tokens volgen die rol meteen."""
    email = _eis_lidmaatschap(project_name, _email(request))
    try:
        await wijzig_rol(project_name=project_name, naam=naam, rol=aanvraag.rol, door=email)
    except TokenAanvraagGeweigerd as exc:
        raise _geweigerd(exc) from exc
    return JSONResponse(content={"status": "gewijzigd", "naam": naam, "rol": aanvraag.rol})


@token_router.post("/projects/{project_name}/leden/{naam}/tokens", status_code=201, summary="Geef een token uit")
@handeling(Handeling.TOKEN_BEHEREN)
@validate_user_token
async def maak_token(
    request: Request, project_name: ProjectNamePath, naam: str, aanvraag: TokenAanvraag = Body(...)
) -> JSONResponse:
    """Geef een token uit aan dit lid. De waarde staat een keer in dit antwoord.

    Een beheerder doet dat voor een service account; een mens doet het voor zichzelf.
    """
    email = _eis_lidmaatschap(project_name, _email(request))
    try:
        waarde = await geef_token_uit(
            project_name=project_name, naam=naam, tokennaam=aanvraag.naam, dagen=aanvraag.dagen, door=email
        )
    except TokenAanvraagGeweigerd as exc:
        raise _geweigerd(exc) from exc
    return JSONResponse(
        content=UitgegevenToken(token=waarde, lid=naam, naam=aanvraag.naam).model_dump(), status_code=201
    )


@token_router.post("/projects/{project_name}/leden/{naam}/tokens/{tokennaam}/:roteer", summary="Roteer een token")
@handeling(Handeling.TOKEN_BEHEREN)
@validate_user_token
async def roteer(
    request: Request, project_name: ProjectNamePath, naam: str, tokennaam: str, aanvraag: TokenAanvraag = Body(...)
) -> JSONResponse:
    """Trek dit token in en geef er een nieuw uit onder dezelfde naam."""
    email = _eis_lidmaatschap(project_name, _email(request))
    try:
        waarde = await roteer_token(
            project_name=project_name, naam=naam, tokennaam=tokennaam, dagen=aanvraag.dagen, door=email
        )
    except TokenAanvraagGeweigerd as exc:
        raise _geweigerd(exc) from exc
    return JSONResponse(content=UitgegevenToken(token=waarde, lid=naam, naam=tokennaam).model_dump())


@token_router.delete("/projects/{project_name}/leden/{naam}/tokens/{tokennaam}", summary="Trek een token in")
@handeling(Handeling.TOKEN_BEHEREN)
@validate_user_token
async def trek_in(request: Request, project_name: ProjectNamePath, naam: str, tokennaam: str) -> JSONResponse:
    """Trek een token in. Dat is een commit, dus het staat in de historie van het project."""
    email = _eis_lidmaatschap(project_name, _email(request))
    try:
        await trek_token_in(project_name=project_name, naam=naam, tokennaam=tokennaam, door=email)
    except TokenAanvraagGeweigerd as exc:
        raise _geweigerd(exc) from exc
    return JSONResponse(content={"status": "ingetrokken", "lid": naam, "naam": tokennaam})
