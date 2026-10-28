"""Webroutes waarmee een gebruiker zijn eigen 2-factor inricht (RC-329).

Zelfbediening: deze pagina's raken alleen de factor van de INGELOGDE gebruiker. De
factor van een ander resetten is beheer en staat in ``router_user_admin.py``.

Een nog niet bevestigde registratie staat niet in de database als factor: de oude factor
verdwijnt pas als de gebruiker een geldige code van de nieuwe heeft ingevoerd. De seed
reist AGE-versleuteld mee in een verborgen veld van het formulier; dat die registratie
loopt, staat met de vingerafdruk van die seed in de tabel ``otp_pending_registrations``.

Server-side, zoals AMT/TAD hetzelfde probleem oploste: tot 27-10-2026 ontving de
sessiecookie die vingerafdruk, en op de sandbox liep zo'n cookie (hij draagt al een
id_token) daarmee over de ~4 KB die een browser geruisloos laat vallen. /bevestigen kwam
dan aan zonder merk, de gebruiker zag geen enkele foutmelding, en de registratie
verdween. De sessie raakt de registratie hier helemaal niet meer; wie geen /start heeft
gedaan, heeft geen rij; de rij verloopt na tien minuten en de cleanup-lus ruimt op.

Een 2-factor inrichten of vervangen vraagt eerst een herlogin bij Keycloak (``/herlogin``):
anders richt iedereen met een open sessie op een ontgrendelde computer een eigen 2-factor in
onder de naam van een ander. De poort staat op ``/start``; ``/bevestigen`` wordt beschermd
door de rij die ``/start`` zet, en zou een registratie vernietigen die al gescand is als het
venster van de herlogin tijdens het scannen verliep.
"""

from __future__ import annotations

import hashlib
import logging

import httpx
from authlib.common.errors import AuthlibBaseError
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from joserfc.errors import JoseError
from starlette.responses import Response

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.authz.actions import Action, action
from opi.services.otp_herlogin_service import OtpHerloginService, herlogin_klopt
from opi.services.user_otp_service import OtpRegistration, OtpVerification, UserOtpService
from opi.utils.csrf import ensure_csrf_token
from opi.utils.qr import qr_svg
from opi.web.lotc_switch import build_lotc_admin, render
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

otp_router = APIRouter(prefix="/account/otp", tags=["account-otp"])

#: Het verborgen veld dat de versleutelde seed draagt.
PENDING_FIELD = "pending"

#: PENDING_TTL van de service bepaalt hoe lang een /start zijn bevestiging accepteert.

_PATH = "/account/otp"


def _get_otp_service() -> UserOtpService:
    return UserOtpService()


def _get_herlogin_service() -> OtpHerloginService:
    return OtpHerloginService()


def _login_at(request: Request) -> float | None:
    login_at = (get_current_user(request) or {}).get("login_at")
    return login_at if isinstance(login_at, int | float) else None


def _herlogin_url(request: Request) -> str:
    return str(request.url_for("otp_herlogin_terug"))


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


async def _status_page(request: Request, context: dict) -> Response:
    herlogin_vers = await _get_herlogin_service().is_fresh(_email(request), _login_at(request))
    return _page(request, "bg/account-otp.html.j2", {"herlogin_vers": herlogin_vers, **context})


def _registration_page(request: Request, registratie: OtpRegistration, fout: str = "", code_fout: str = "") -> Response:
    return _page(
        request,
        "bg/account-otp-registreren.html.j2",
        {
            "qr_svg": qr_svg(registratie.otpauth_uri),
            "base32_secret": registratie.base32_secret,
            "pending": registratie.encrypted_secret,
            "pending_field": PENDING_FIELD,
            "fout": fout,
            "code_fout": code_fout,
        },
    )


@otp_router.get("", response_class=HTMLResponse)
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_status_pagina(request: Request) -> Response:
    """Of je een 2-factor hebt, en de weg om hem in te richten of te vervangen."""
    status = await _get_otp_service().status(_email(request))
    return await _status_page(request, {"status": status})


@otp_router.post("/start", response_model=None)
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_start(request: Request) -> Response:
    """Maak een seed en toon de QR. Schrijft nog niets in de database.

    Een BESTAANDE, bevestigde factor vervangen vraagt eerst een code van die factor:
    anders neemt wie een sessie overneemt ook de factor over. Wie die code niet heeft,
    vraagt een platformbeheerder om een reset.
    """
    email = _email(request)
    if not await _get_herlogin_service().is_fresh(email, _login_at(request)):
        return RedirectResponse(url=f"{_PATH}/herlogin", status_code=303)

    service = _get_otp_service()
    status = await service.status(email)

    if status.registered:
        form = await request.form()
        uitkomst = await service.verify(email, str(form.get("otp", "")))
        if uitkomst is not OtpVerification.OK:
            return await _status_page(
                request,
                {
                    "status": status,
                    "code_fout": (
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


@otp_router.get("/herlogin", response_model=None)
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_herlogin(request: Request) -> Response:
    """Stuur naar Keycloak voor een verse login, voordat een 2-factor ingericht wordt.

    ``prompt=login`` en ``max_age=0`` dwingen een nieuwe authenticatie af, ook als Keycloak
    nog een sessie van deze browser kent. De state staat server-side (zie
    ``opi/services/persistence/otp_herlogin.py``).
    """
    login_at = _login_at(request)
    if login_at is None:
        raise HTTPException(status_code=403, detail="Geen geldige sessie om opnieuw mee in te loggen")

    herlogin = await _get_herlogin_service().begin(_email(request), login_at)
    keycloak = request.app.state.oauth.keycloak
    aanvraag = await keycloak.create_authorization_url(
        _herlogin_url(request),
        state=herlogin.state,
        nonce=herlogin.nonce,
        code_verifier=herlogin.code_verifier,
        prompt="login",
        max_age=0,
    )
    return RedirectResponse(url=aanvraag["url"], status_code=302)


@otp_router.get("/herlogin/terug", response_model=None, name="otp_herlogin_terug")
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_herlogin_terug(request: Request) -> Response:
    """Keycloak komt hier terug. Een geslaagde herlogin opent het venster voor ``/start``."""
    email = _email(request)
    login_at = _login_at(request)
    code = request.query_params.get("code", "")
    service = _get_herlogin_service()
    herlogin = (
        await service.take(request.query_params.get("state", ""), email, login_at) if login_at is not None else None
    )

    if herlogin is None or not code:
        logger.warning("Herlogin zonder geldige state of code voor %s", email)
    else:
        keycloak = request.app.state.oauth.keycloak
        try:
            token = await keycloak.fetch_access_token(
                _herlogin_url(request), code=code, code_verifier=herlogin.code_verifier
            )
            claims = await keycloak.parse_id_token(token, herlogin.nonce)
        except (AuthlibBaseError, JoseError, httpx.HTTPError) as e:
            logger.warning("Herlogin voor %s mislukt bij Keycloak: %s", email, e)
        else:
            if herlogin_klopt(claims, get_current_user(request) or {}):
                await service.mark_verified(herlogin.state)
                return RedirectResponse(url=_PATH, status_code=303)

    return await _status_page(
        request,
        {
            "status": await _get_otp_service().status(email),
            "fout": "Opnieuw inloggen is niet gelukt. Probeer het opnieuw.",
        },
    )


@otp_router.post("/bevestigen", response_model=None)
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_bevestigen(request: Request) -> Response:
    """Bevestig de lopende registratie. Pas hier gaat de nieuwe 2-factor in."""
    form = await request.form()
    # Een formulier-POST maakt CRLF van de LF's in het AGE-pantser (form-encoding);
    # de afdruk en de ontsleuteling horen de kanonieke vorm te zien.
    encrypted = str(form.get(PENDING_FIELD, "")).replace("\r\n", "\n")
    email = _email(request)
    if not encrypted or not await _get_otp_service().consume_pending_registration(email, _fingerprint(encrypted)):
        logger.warning("OTP-bevestiging zonder lopende registratie voor %s", email)
        service = _get_otp_service()
        if encrypted and await service.renew_expired_pending_registration(email, _fingerprint(encrypted)):
            return _registration_page(
                request,
                await service.resume_registration(email, encrypted),
                fout=(
                    "De registratie was verlopen. Probeer het nogmaals: scan de QR niet opnieuw, "
                    "maar vul een nieuwe code uit je app in."
                ),
            )
        return await _status_page(
            request,
            {
                "status": await _get_otp_service().status(email),
                "fout": "Die registratie was verlopen of is al afgebroken. Begin opnieuw.",
            },
        )

    user = get_current_user(request) or {}
    service = _get_otp_service()
    bevestigd = await service.confirm_registration(
        email,
        str(form.get("otp", "")),
        encrypted,
        full_name=str(user.get("name") or user.get("display_name") or ""),
    )
    if not bevestigd:
        registratie = await service.resume_registration(email, encrypted)
        return _registration_page(
            request, registratie, code_fout="Die code klopt niet. Probeer de volgende code uit je app."
        )

    return RedirectResponse(url=_PATH, status_code=303)


@otp_router.post("/afbreken", response_model=None)
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_afbreken(request: Request) -> Response:
    """Vergeet de lopende registratie. De bestaande factor blijft zoals hij was."""
    await _get_otp_service().discard_pending_registration(_email(request))
    return RedirectResponse(url=_PATH, status_code=303)
