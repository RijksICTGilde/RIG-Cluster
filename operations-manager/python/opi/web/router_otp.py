"""Web routes with which a user sets up their own second factor.

Self-service: these pages only touch the factor of the SIGNED-IN user. Resetting someone
else's factor is administration and lives in ``router_user_admin.py``.

A registration that has not been confirmed is not a factor in the database: the old factor goes
only once the user has entered a valid code from the new one. The seed travels AGE-encrypted in
a hidden form field, and that the registration is running is recorded by the fingerprint of
that seed in the table ``otp_pending_registrations``. The session does not carry the
registration at all: whoever did no ``/start`` has no row, the row expires after ten minutes,
and the cleanup loop clears it.

Setting up or replacing a factor asks for a re-login at Keycloak first, or anyone with an open
session on an unlocked computer sets up a factor in someone else's name. That gate sits on
``/start`` only: ``/bevestigen`` is protected by the row ``/start`` writes, and gating it too
would destroy a registration that has already been scanned when the re-login window ran out
during the scanning.
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

from opi.core.authz.actions import Action, action
from opi.core.request_user import get_current_user
from opi.http.auth.auth_decorators import requires_sso
from opi.http.render.lotc_switch import build_lotc_admin, render
from opi.http.render.menu import get_menu_items
from opi.services.otp_herlogin_service import OtpHerloginService, herlogin_is_valid
from opi.services.user_otp_service import OtpRegistration, OtpVerification, UserOtpService
from opi.utils.csrf import ensure_csrf_token
from opi.utils.qr import qr_svg

logger = logging.getLogger(__name__)

otp_router = APIRouter(prefix="/account/otp", tags=["account-otp"])

#: The hidden field that carries the encrypted seed.
PENDING_FIELD = "pending"

#: ``PENDING_TTL`` of the service decides how long a /start accepts its confirmation.

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
    """One page of this series, with the shell and the navigation around it."""
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


def _registration_page(
    request: Request, registration: OtpRegistration, error: str = "", code_error: str = ""
) -> Response:
    return _page(
        request,
        "bg/account-otp-registreren.html.j2",
        {
            "qr_svg": qr_svg(registration.otpauth_uri),
            "base32_secret": registration.base32_secret,
            "pending": registration.encrypted_secret,
            "pending_field": PENDING_FIELD,
            "fout": error,
            "code_fout": code_error,
        },
    )


@otp_router.get("", response_class=HTMLResponse)
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_status_pagina(request: Request) -> Response:
    """Whether you have a second factor, and the way to set one up or replace it."""
    status = await _get_otp_service().status(_email(request))
    return await _status_page(request, {"status": status})


@otp_router.post("/start", response_model=None)
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_start(request: Request) -> Response:
    """Make a seed and show the QR; writes nothing in the database yet.

    Replacing an EXISTING, confirmed factor asks for a code from that factor first, or whoever
    takes over a session takes over the factor as well. Without that code, a platform admin
    resets the factor.
    """
    email = _email(request)
    if not await _get_herlogin_service().is_fresh(email, _login_at(request)):
        return RedirectResponse(url=f"{_PATH}/herlogin", status_code=303)

    service = _get_otp_service()
    status = await service.status(email)

    if status.registered:
        form = await request.form()
        outcome = await service.verify(email, str(form.get("otp", "")))
        if outcome is not OtpVerification.OK:
            return await _status_page(
                request,
                {
                    "status": status,
                    "code_fout": (
                        "Je factor is tijdelijk geblokkeerd na te veel mislukte pogingen. Probeer het later "
                        "opnieuw, of vraag een platformbeheerder om je factor te resetten."
                        if outcome is OtpVerification.LOCKED
                        else "Die code klopt niet. Vul een code uit je huidige app in."
                    ),
                },
            )

    registration = await service.start_registration(email)
    await service.note_pending_registration(email, _fingerprint(registration.encrypted_secret))
    return _registration_page(request, registration)


@otp_router.get("/herlogin", response_model=None)
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_herlogin(request: Request) -> Response:
    """Send to Keycloak for a fresh login, before a second factor is set up.

    ``prompt=login`` and ``max_age=0`` force a new authentication even when Keycloak still
    knows a session of this browser. The state is kept server-side.
    """
    login_at = _login_at(request)
    if login_at is None:
        raise HTTPException(status_code=403, detail="Geen geldige sessie om opnieuw mee in te loggen")

    herlogin = await _get_herlogin_service().begin(_email(request), login_at)
    keycloak = request.app.state.oauth.keycloak
    authorization = await keycloak.create_authorization_url(
        _herlogin_url(request),
        state=herlogin.state,
        nonce=herlogin.nonce,
        code_verifier=herlogin.code_verifier,
        prompt="login",
        max_age=0,
    )
    return RedirectResponse(url=authorization["url"], status_code=302)


@otp_router.get("/herlogin/terug", response_model=None, name="otp_herlogin_terug")
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_herlogin_terug(request: Request) -> Response:
    """Keycloak comes back here; a successful re-login opens the window for ``/start``."""
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
            if herlogin_is_valid(claims, get_current_user(request) or {}):
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
    """Confirm the running registration; only here does the new factor take effect."""
    form = await request.form()
    # A form POST turns the LFs in the AGE armour into CRLFs, and both the fingerprint and
    # the decryption have to see the canonical form.
    encrypted = str(form.get(PENDING_FIELD, "")).replace("\r\n", "\n")
    email = _email(request)
    if not encrypted or not await _get_otp_service().consume_pending_registration(email, _fingerprint(encrypted)):
        logger.warning("OTP-bevestiging zonder lopende registratie voor %s", email)
        service = _get_otp_service()
        if encrypted and await service.renew_expired_pending_registration(email, _fingerprint(encrypted)):
            return _registration_page(
                request,
                await service.resume_registration(email, encrypted),
                error=(
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
    confirmed = await service.confirm_registration(
        email,
        str(form.get("otp", "")),
        encrypted,
        full_name=str(user.get("name") or user.get("display_name") or ""),
    )
    if not confirmed:
        registration = await service.resume_registration(email, encrypted)
        return _registration_page(
            request, registration, code_error="Die code klopt niet. Probeer de volgende code uit je app."
        )

    return RedirectResponse(url=_PATH, status_code=303)


@otp_router.post("/afbreken", response_model=None)
@action(Action.ACCOUNT_OTP)
@requires_sso
async def otp_afbreken(request: Request) -> Response:
    """Forget the running registration; the existing factor stays as it was."""
    await _get_otp_service().discard_pending_registration(_email(request))
    return RedirectResponse(url=_PATH, status_code=303)
