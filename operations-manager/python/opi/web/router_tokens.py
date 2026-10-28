"""Leden en tokens beheren vanuit het portaal.

Dezelfde regels als de API: ze staan in :mod:`opi.services.api_token_service` en niet hier,
zodat het formulier en de API hetzelfde logicapad lopen.

De uitgegeven waarde komt EEN keer terug en wordt bij het tonen meteen weggegooid. Een
token dat je op de pagina kunt terugvinden is geen token dat niemand meer kan lezen.

De waarde zelf blijft daarbij server-side: de sessie draagt alleen een verwijzing. De
cookie-sessie van Starlette is ondertekend en niet versleuteld, dus het token zou tot de
volgende GET base64-leesbaar op de schijf van de browser staan. Zelfde keuze als
``router_otp``, dat ook een vingerafdruk in de sessie zet en niet het geheim.

Elke handeling hier vraagt een verse step-up (RC-387), met het venster van vijf minuten uit
``opi.core.step_up``. De dialoog neemt de plaats van het formulier in, dus de velden van de
handeling gaan mee in de herhaling: ``velden`` op de decorator somt ze per route op.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from opi.core.auth_decorators import get_current_user, requires_sso, requires_step_up
from opi.core.authz.actions import Action, action
from opi.core.authz.engine import ROLE_DESCRIPTIONS, Role, role_label
from opi.core.config import settings
from opi.core.leden import LidSoort
from opi.core.step_up import StepUpAction
from opi.services.api_token_service import (
    STANDAARD_ROLLEN,
    TokenAanvraagGeweigerd,
    geef_token_uit,
    leden_overzicht,
    roteer_token,
    set_member_roles,
    trek_token_in,
    verwijder_service_account,
    voeg_service_account_toe,
)
from opi.web.lotc_switch import project_tab_url

logger = logging.getLogger(__name__)

token_web_router = APIRouter(prefix="/projects", tags=["tokens-web"])

#: Waar de verwijzing naar het nieuwe token en een eventuele weigering tussen de POST en
#: de volgende GET staan.
NIEUW_TOKEN_SLEUTEL = "nieuw_token"  # noqa: S105 -- sessiesleutel, geen secret
TOKENFOUT_SLEUTEL = "tokenfout"

#: Hoe lang een uitgegeven waarde op zijn eenmalige weergave wacht. Het normale pad is de
#: 303 die er meteen naartoe gaat; wie het tabblad niet opent laat hem hier verlopen.
_WEERGAVE_SECONDEN = 300

#: De uitgegeven waarden die nog op hun eenmalige weergave wachten, per verwijzing. OPI
#: draait als een enkele replica, dus dit hoeft niet gedeeld te zijn; een herstart tussen
#: de POST en de GET kost de weergave, en dan is het token te roteren.
_wachtend: dict[str, tuple[float, dict[str, str]]] = {}


def _leg_klaar(details: dict[str, str]) -> str:
    """Bewaar een uitgegeven waarde server-side en geef de verwijzing terug."""
    nu = time.monotonic()
    for verwijzing, (vervalt, _) in list(_wachtend.items()):
        if vervalt <= nu:
            del _wachtend[verwijzing]
    verwijzing = secrets.token_urlsafe(16)
    _wachtend[verwijzing] = (nu + _WEERGAVE_SECONDEN, details)
    return verwijzing


def _haal_op(verwijzing: str | None) -> dict[str, str] | None:
    """De waarde achter deze verwijzing, een keer. Daarna is hij weg."""
    if not verwijzing:
        return None
    wachtend = _wachtend.pop(verwijzing, None)
    if wachtend is None:
        return None
    vervalt, details = wachtend
    return details if vervalt > time.monotonic() else None


def _email(request: Request) -> str:
    gebruiker = get_current_user(request) or {}
    email = str(gebruiker.get("email", "")).lower()
    if not email:
        raise HTTPException(status_code=403, detail="Geen aangemelde gebruiker")
    return email


def _terug(request: Request, project_name: str) -> Response:
    """Terug naar het tabblad Team, en onder htmx als volledige navigatie.

    De formulieren posten met htmx omdat de step-up een dialoog in de plaats van het
    formulier zet. Een 303 zou htmx de hele pagina in die dialoog laten hangen, dus gaat
    de verversing als HX-Redirect; zie bg/_tokens.html.j2.
    """
    url = project_tab_url(project_name, "team")
    if request.headers.get("HX-Request"):
        return HTMLResponse(content="", headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)


async def _velden(request: Request) -> dict[str, str]:
    """Het formulier als vlakke dict; een meerkeuze wordt een kommalijst.

    Dezelfde vorm als de step-up-herhaling teruggeeft (``_velden`` in auth_decorators),
    zodat een route niet hoeft te weten of hij het formulier of de herhaling leest.
    """
    formulier = await request.form()
    getlist = getattr(formulier, "getlist", None)
    if getlist is None:
        # Een test die een gewone dict als formulier levert.
        return {sleutel: str(waarde) for sleutel, waarde in formulier.items()}
    return {sleutel: ",".join(str(waarde) for waarde in getlist(sleutel)) for sleutel in formulier}


def _rollen_uit(velden: dict[str, str]) -> list[str]:
    """De gekozen rollen uit het formulier.

    De meerkeuze stuurt ``roles`` meerdere keren; ``_velden`` heeft die dan al tot een
    kommalijst samengevoegd, en de step-up-herhaling stuurt diezelfde vorm terug.
    """
    return [deel.strip() for deel in velden.get("roles", "").split(",") if deel.strip()]


def _dagen(velden: dict[str, str]) -> int:
    try:
        return int(velden.get("dagen") or "0")
    except ValueError as exc:
        raise TokenAanvraagGeweigerd("Vul een aantal dagen in.") from exc


@token_web_router.post("/{project_name}/leden/toevoegen")
@action(Action.MEMBER_MANAGE)
@requires_sso
@requires_step_up(actie=StepUpAction.SERVICE_ACCOUNT_ADD, velden=("naam", "roles"))
async def voeg_service_account_toe_web(request: Request, project_name: str) -> Response:
    """Zet een service account in de leden van dit project."""
    velden = await _velden(request)
    try:
        await voeg_service_account_toe(
            project_name=project_name,
            naam=velden.get("naam", ""),
            rollen=_rollen_uit(velden) or list(STANDAARD_ROLLEN),
            door=_email(request),
        )
    except TokenAanvraagGeweigerd as exc:
        request.session[TOKENFOUT_SLEUTEL] = str(exc)
    return _terug(request, project_name)


@token_web_router.post("/{project_name}/leden/{naam}/verwijderen")
@action(Action.MEMBER_MANAGE)
@requires_sso
@requires_step_up(actie=StepUpAction.SERVICE_ACCOUNT_REMOVE)
async def verwijder_service_account_web(request: Request, project_name: str, naam: str) -> Response:
    """Haal een service account uit de leden. Zijn tokens gaan dezelfde commit mee uit."""
    try:
        await verwijder_service_account(project_name=project_name, naam=naam, door=_email(request))
    except TokenAanvraagGeweigerd as exc:
        request.session[TOKENFOUT_SLEUTEL] = str(exc)
    return _terug(request, project_name)


@token_web_router.post("/{project_name}/leden/{naam}/rol")
@action(Action.MEMBER_MANAGE)
@requires_sso
@requires_step_up(actie=StepUpAction.ROLE_CHANGE, velden=("roles",))
async def wijzig_rol_web(request: Request, project_name: str, naam: str) -> Response:
    """Geef een lid andere rollen. Zijn bestaande tokens volgen die rollen meteen."""
    velden = await _velden(request)
    try:
        await set_member_roles(project_name=project_name, naam=naam, rollen=_rollen_uit(velden), door=_email(request))
    except TokenAanvraagGeweigerd as exc:
        request.session[TOKENFOUT_SLEUTEL] = str(exc)
    return _terug(request, project_name)


@token_web_router.post("/{project_name}/leden/{naam}/tokens")
@action(Action.TOKEN_MANAGE)
@requires_sso
@requires_step_up(actie=StepUpAction.TOKEN_ISSUE, velden=("tokennaam", "dagen"))
async def maak_token_web(request: Request, project_name: str, naam: str) -> Response:
    """Geef een token uit aan dit lid. De waarde is daarna een keer te zien."""
    velden = await _velden(request)
    try:
        waarde = await geef_token_uit(
            project_name=project_name,
            naam=naam,
            tokennaam=velden.get("tokennaam", ""),
            dagen=_dagen(velden),
            door=_email(request),
        )
    except TokenAanvraagGeweigerd as exc:
        request.session[TOKENFOUT_SLEUTEL] = str(exc)
        return _terug(request, project_name)

    request.session[NIEUW_TOKEN_SLEUTEL] = _leg_klaar({"token": waarde, "eigenaar": naam})
    return _terug(request, project_name)


@token_web_router.post("/{project_name}/leden/{naam}/tokens/{tokennaam}/roteren")
@action(Action.TOKEN_MANAGE)
@requires_sso
@requires_step_up(actie=StepUpAction.TOKEN_ROTATE, velden=("dagen",))
async def roteer_token_web(request: Request, project_name: str, naam: str, tokennaam: str) -> Response:
    """Trek dit token in en geef er een nieuw uit onder dezelfde naam."""
    velden = await _velden(request)
    try:
        waarde = await roteer_token(
            project_name=project_name,
            naam=naam,
            tokennaam=tokennaam,
            dagen=_dagen(velden),
            door=_email(request),
        )
    except TokenAanvraagGeweigerd as exc:
        request.session[TOKENFOUT_SLEUTEL] = str(exc)
        return _terug(request, project_name)

    request.session[NIEUW_TOKEN_SLEUTEL] = _leg_klaar({"token": waarde, "eigenaar": naam})
    return _terug(request, project_name)


@token_web_router.post("/{project_name}/leden/{naam}/tokens/{tokennaam}/intrekken")
@action(Action.TOKEN_MANAGE)
@requires_sso
@requires_step_up(actie=StepUpAction.TOKEN_REVOKE)
async def trek_token_in_web(request: Request, project_name: str, naam: str, tokennaam: str) -> Response:
    """Trek een token in. Dat is een commit, dus het staat in de historie van het project."""
    try:
        await trek_token_in(project_name=project_name, naam=naam, tokennaam=tokennaam, door=_email(request))
    except TokenAanvraagGeweigerd as exc:
        request.session[TOKENFOUT_SLEUTEL] = str(exc)
    return _terug(request, project_name)


def tokencontext(request: Request, project_name: str, user_email: str, is_beheerder: bool) -> dict[str, Any]:
    """Wat het tabblad Team over leden en tokens moet tonen.

    Leest alleen uit het projectbestand, dus er is geen database bij en geen tak voor een
    database die niet meebeweegt.
    """
    leden = leden_overzicht(project_name)
    for lid in leden:
        lid["is_service_account"] = lid["soort"] == LidSoort.SERVICE_ACCOUNT.value
        lid["is_current_user"] = lid["naam"].lower() == user_email
        # Een beheerder beheert de tokens van elk lid; een mens die van zichzelf. Uitgeven
        # voor een ander mens kan niemand: dat zou een bewijsmiddel zijn dat iemand draagt
        # zonder te weten dat het bestaat.
        lid["mag_uitgeven"] = (lid["is_service_account"] and is_beheerder) or lid["is_current_user"]
        lid["mag_intrekken"] = is_beheerder or lid["is_current_user"]

    # The caller's own card goes first; the stable sort keeps the project file order for
    # the rest, and a caller without membership moves nothing.
    leden.sort(key=lambda lid: not lid["is_current_user"])

    return {
        "leden": leden,
        # Label en omschrijving uit het register (B13): een andere naam op het scherm
        # is dan een wijziging op een plek, en geen migratie van projectbestanden.
        "rollen": [{"sleutel": rol.value, "label": role_label(rol), "uitleg": ROLE_DESCRIPTIONS[rol]} for rol in Role],
        "standaard_rollen": list(STANDAARD_ROLLEN),
        "is_project_beheerder": is_beheerder,
        "token_max_dagen_persoonlijk": settings.TOKEN_MAX_DAGEN_PERSOONLIJK,
        "token_max_dagen_service_account": settings.TOKEN_MAX_DAGEN_SERVICE_ACCOUNT,
        "nieuw_token": _haal_op(request.session.pop(NIEUW_TOKEN_SLEUTEL, None)),
        "tokenfout": request.session.pop(TOKENFOUT_SLEUTEL, None),
    }
