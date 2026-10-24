"""Leden en tokens beheren vanuit het portaal.

Dezelfde regels als de API: ze staan in :mod:`opi.services.api_token_service` en niet hier,
zodat het formulier en de API hetzelfde logicapad lopen.

De uitgegeven waarde komt EEN keer terug en wordt bij het tonen meteen weggegooid. Een
token dat je op de pagina kunt terugvinden is geen token dat niemand meer kan lezen.

De waarde zelf blijft daarbij server-side: de sessie draagt alleen een verwijzing. De
cookie-sessie van Starlette is ondertekend en niet versleuteld, dus het token zou tot de
volgende GET base64-leesbaar op de schijf van de browser staan. Zelfde keuze als
``router_otp``, dat ook een vingerafdruk in de sessie zet en niet het geheim.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.config import settings
from opi.core.leden import LidSoort
from opi.services.api_token_service import (
    ROLLEN,
    TokenAanvraagGeweigerd,
    geef_token_uit,
    leden_overzicht,
    roteer_token,
    trek_token_in,
    verwijder_service_account,
    voeg_service_account_toe,
    wijzig_rol,
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


def _terug(project_name: str) -> RedirectResponse:
    return RedirectResponse(url=project_tab_url(project_name, "team"), status_code=303)


async def _velden(request: Request) -> dict[str, str]:
    """Het formulier als vlakke dict. Geen veld komt hier meer dan een keer voorbij."""
    return {sleutel: str(waarde) for sleutel, waarde in (await request.form()).items()}


def _dagen(velden: dict[str, str]) -> int:
    try:
        return int(velden.get("dagen") or "0")
    except ValueError as exc:
        raise TokenAanvraagGeweigerd("Vul een aantal dagen in.") from exc


@token_web_router.post("/{project_name}/leden/toevoegen")
@requires_sso
async def voeg_service_account_toe_web(request: Request, project_name: str) -> RedirectResponse:
    """Zet een service account in de leden van dit project."""
    velden = await _velden(request)
    try:
        await voeg_service_account_toe(
            project_name=project_name,
            naam=velden.get("naam", ""),
            rol=velden.get("rol", "developer"),
            door=_email(request),
        )
    except TokenAanvraagGeweigerd as exc:
        request.session[TOKENFOUT_SLEUTEL] = str(exc)
    return _terug(project_name)


@token_web_router.post("/{project_name}/leden/{naam}/verwijderen")
@requires_sso
async def verwijder_service_account_web(request: Request, project_name: str, naam: str) -> RedirectResponse:
    """Haal een service account uit de leden. Zijn tokens gaan dezelfde commit mee uit."""
    try:
        await verwijder_service_account(project_name=project_name, naam=naam, door=_email(request))
    except TokenAanvraagGeweigerd as exc:
        request.session[TOKENFOUT_SLEUTEL] = str(exc)
    return _terug(project_name)


@token_web_router.post("/{project_name}/leden/{naam}/rol")
@requires_sso
async def wijzig_rol_web(request: Request, project_name: str, naam: str) -> RedirectResponse:
    """Geef een lid een andere rol. Zijn bestaande tokens volgen die rol meteen."""
    velden = await _velden(request)
    try:
        await wijzig_rol(project_name=project_name, naam=naam, rol=velden.get("rol", ""), door=_email(request))
    except TokenAanvraagGeweigerd as exc:
        request.session[TOKENFOUT_SLEUTEL] = str(exc)
    return _terug(project_name)


@token_web_router.post("/{project_name}/leden/{naam}/tokens")
@requires_sso
async def maak_token_web(request: Request, project_name: str, naam: str) -> RedirectResponse:
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
        return _terug(project_name)

    request.session[NIEUW_TOKEN_SLEUTEL] = _leg_klaar({"token": waarde, "eigenaar": naam})
    return _terug(project_name)


@token_web_router.post("/{project_name}/leden/{naam}/tokens/{tokennaam}/roteren")
@requires_sso
async def roteer_token_web(request: Request, project_name: str, naam: str, tokennaam: str) -> RedirectResponse:
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
        return _terug(project_name)

    request.session[NIEUW_TOKEN_SLEUTEL] = _leg_klaar({"token": waarde, "eigenaar": naam})
    return _terug(project_name)


@token_web_router.post("/{project_name}/leden/{naam}/tokens/{tokennaam}/intrekken")
@requires_sso
async def trek_token_in_web(request: Request, project_name: str, naam: str, tokennaam: str) -> RedirectResponse:
    """Trek een token in. Dat is een commit, dus het staat in de historie van het project."""
    try:
        await trek_token_in(project_name=project_name, naam=naam, tokennaam=tokennaam, door=_email(request))
    except TokenAanvraagGeweigerd as exc:
        request.session[TOKENFOUT_SLEUTEL] = str(exc)
    return _terug(project_name)


def tokencontext(request: Request, project_name: str, user_email: str, is_beheerder: bool) -> dict[str, Any]:
    """Wat het tabblad Team over leden en tokens moet tonen.

    Leest alleen uit het projectbestand, dus er is geen database bij en geen tak voor een
    database die niet meebeweegt.
    """
    leden = leden_overzicht(project_name)
    for lid in leden:
        lid["is_service_account"] = lid["soort"] == LidSoort.SERVICE_ACCOUNT.value
        # Een beheerder beheert de tokens van elk lid; een mens die van zichzelf. Uitgeven
        # voor een ander mens kan niemand: dat zou een bewijsmiddel zijn dat iemand draagt
        # zonder te weten dat het bestaat.
        lid["mag_uitgeven"] = (lid["is_service_account"] and is_beheerder) or lid["naam"].lower() == user_email
        lid["mag_intrekken"] = is_beheerder or lid["naam"].lower() == user_email

    return {
        "leden": leden,
        "rollen": ROLLEN,
        "is_project_beheerder": is_beheerder,
        "token_max_dagen_persoonlijk": settings.TOKEN_MAX_DAGEN_PERSOONLIJK,
        "token_max_dagen_service_account": settings.TOKEN_MAX_DAGEN_SERVICE_ACCOUNT,
        "nieuw_token": _haal_op(request.session.pop(NIEUW_TOKEN_SLEUTEL, None)),
        "tokenfout": request.session.pop(TOKENFOUT_SLEUTEL, None),
    }
