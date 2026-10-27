"""Webroutes waarmee een gebruiker zijn eigen tweede factor inricht (RC-329).

Zelfbediening: deze pagina's raken alleen de factor van de INGELOGDE gebruiker. De
factor van een ander resetten is beheer en staat in ``router_user_admin.py``.

Een nog niet bevestigde registratie staat niet in de database als factor: de oude factor
verdwijnt pas als de gebruiker een geldige code van de nieuwe heeft ingevoerd. Het zaad
reist AGE-versleuteld mee in een verborgen veld van het formulier; dat die registratie
loopt, staat met de vingerafdruk van dat zaad in de tabel ``otp_pending_registrations``.

Server-side, zoals AMT/TAD hetzelfde probleem oploste: tot 27-10-2026 ontving de
sessiecookie die vingerafdruk, en op de sandbox liep zo'n cookie (hij draagt al een
id_token) daarmee over de ~4 KB die een browser geruisloos laat vallen. /bevestigen kwam
dan aan zonder merk, de gebruiker zag geen enkele foutmelding, en de registratie
verdween. De sessie raakt de registratie hier helemaal niet meer; wie geen /start heeft
gedaan, heeft geen rij; de rij verloopt na tien minuten en de cleanup-lus ruimt op.
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.services.user_otp_service import OtpRegistration, OtpVerification, UserOtpService
from opi.utils.csrf import ensure_csrf_token
from opi.utils.qr import qr_svg
from opi.web.lotc_switch import build_lotc_admin, render
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

otp_router = APIRouter(prefix="/account/otp", tags=["account-otp"])

#: Het verborgen veld dat het versleutelde zaad draagt.
PENDING_FIELD = "pending"

#: PENDING_TTL van de service bepaalt hoe lang een /start zijn bevestiging accepteert.

_PATH = "/account/otp"


def _get_otp_service() -> UserOtpService:
    return UserOtpService()


def _email(request: Request) -> str:
    user = get_current_user(request) or {}
    return str(user.get("email", "")).lower()


def _fingerprint(encrypted_secret: str) -> str:
    return hashlib.sha256(encrypted_secret.encode()).hexdigest()


def _page(request: Request, template: str, context: dict) -> Response:
    """Een pagina van deze reeks, met de schil en de navigatie eromheen."""
    user = get_current_user(request)
    return render(
        request,
        template=template,
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "csrf_token": ensure_csrf_token(request),
            **build_lotc_admin(user=user, current_path="/account"),
            **context,
        },
    )


def _status_page(request: Request, context: dict) -> Response:
    return _page(request, "bg/account-otp.html.j2", context)


def _registration_page(request: Request, registratie: OtpRegistration, fout: str = "") -> Response:
    return _page(
        request,
        "bg/account-otp-registreren.html.j2",
        {
            "qr_svg": qr_svg(registratie.otpauth_uri),
            "base32_secret": registratie.base32_secret,
            "pending": registratie.encrypted_secret,
            "pending_field": PENDING_FIELD,
            "fout": fout,
        },
    )


@otp_router.get("", response_class=HTMLResponse)
@requires_sso
async def otp_status_pagina(request: Request) -> Response:
    """Of je een tweede factor hebt, en de weg om hem in te richten of te vervangen."""
    status = await _get_otp_service().status(_email(request))
    return _status_page(request, {"status": status, "melding": request.query_params.get("melding")})


@otp_router.post("/start", response_model=None)
@requires_sso
async def otp_start(request: Request) -> Response:
    """Maak een zaad en toon de QR. Schrijft nog niets in de database.

    Een BESTAANDE, bevestigde factor vervangen vraagt eerst een code van die factor:
    anders neemt wie een sessie overneemt ook de factor over. Wie die code niet heeft,
    vraagt een platformbeheerder om een reset.
    """
    email = _email(request)
    service = _get_otp_service()
    status = await service.status(email)

    if status.registered:
        form = await request.form()
        uitkomst = await service.verify(email, str(form.get("huidige_code", "")))
        if uitkomst is not OtpVerification.OK:
            return _status_page(
                request,
                {
                    "status": status,
                    "fout": (
                        "Je factor is tijdelijk geblokkeerd na te veel mislukte pogingen. Probeer het later "
                        "opnieuw, of vraag een platformbeheerder om je factor te resetten."
                        if uitkomst is OtpVerification.LOCKED
                        else "Die code klopt niet. Vul een code uit je huidige app in."
                    ),
                },
            )

    registratie = await service.start_registration(email)
    await service.note_pending_registration(email, _fingerprint(registratie.encrypted_secret))
    return _registration_page(request, registratie)


@otp_router.post("/bevestigen", response_model=None)
@requires_sso
async def otp_bevestigen(request: Request) -> Response:
    """Bevestig de lopende registratie. Pas hier gaat de nieuwe factor in."""
    form = await request.form()
    # Een formulier-POST maakt CRLF van de LF's in het AGE-pantser (form-encoding);
    # de afdruk en de ontsleuteling horen de kanonieke vorm te zien.
    encrypted = str(form.get(PENDING_FIELD, "")).replace("\r\n", "\n")
    email = _email(request)
    if not encrypted or not await _get_otp_service().consume_pending_registration(email, _fingerprint(encrypted)):
        logger.warning("OTP-bevestiging zonder lopende registratie voor %s", email)
        # Stil wegvallen was de bug van 27-10: de gebruiker keek op "niets gebeurd". Zeg
        # in dat geval wat er aan de hand is: de getoonde afspraak was verlopen of nooit van
        # deze start, en de weg terug is opnieuw beginnen.
        return RedirectResponse(
            url=f"{_PATH}?melding=Die+registratie+was+verlopen+of+is+al+afgebroken.+Begin+opnieuw.",
            status_code=303,
        )

    user = get_current_user(request) or {}
    service = _get_otp_service()
    bevestigd = await service.confirm_registration(
        email,
        str(form.get("code", "")),
        encrypted,
        full_name=str(user.get("name") or user.get("display_name") or ""),
    )
    if not bevestigd:
        registratie = await service.resume_registration(email, encrypted)
        return _registration_page(
            request, registratie, fout="Die code klopt niet. Probeer de volgende code uit je app."
        )

    return RedirectResponse(url=f"{_PATH}?melding=Tweede+factor+ingericht", status_code=303)


@otp_router.post("/afbreken", response_model=None)
@requires_sso
async def otp_afbreken(request: Request) -> Response:
    """Vergeet de lopende registratie. De bestaande factor blijft zoals hij was."""
    await _get_otp_service().discard_pending_registration(_email(request))
    return RedirectResponse(url=f"{_PATH}?melding=Registratie+afgebroken", status_code=303)
