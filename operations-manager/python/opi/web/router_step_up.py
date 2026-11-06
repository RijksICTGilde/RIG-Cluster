"""De verse step-up in het portaal (RC-332).

Een handeling die productie raakt wisselt zichzelf in voor een dialoog met een OTP-veld.
Die dialoog post hier; bij een geldige code staat de step-up in de sessie en voert de
dialoog de oorspronkelijke handeling alsnog uit.

De route valt onder de centrale CSRF-bescherming: hij staat niet onder ``/api/``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from opi.api.router import sanitize_for_log
from opi.core.auth_decorators import ELEVATE_PATH, elevate_url, get_current_user, requires_sso
from opi.core.authz.actions import Action, action
from opi.core.step_up import STEP_UP_WINDOW, clear_step_up, record_step_up, step_up_until
from opi.services.audit import record_step_up_attempt
from opi.services.user_otp_service import OtpVerification, UserOtpService
from opi.utils.client_ip import client_ip
from opi.utils.url_paths import local_redirect_path
from opi.web.lotc_switch import render_fragment
from opi.web.step_up_dialog import lees_vervolg_velden, render_step_up_dialog, render_step_up_repeat

logger = logging.getLogger(__name__)

step_up_router = APIRouter(tags=["step-up"])


def _code_error(uitkomst: OtpVerification) -> str:
    if uitkomst is OtpVerification.LOCKED:
        return "Je factor is tijdelijk geblokkeerd na te veel mislukte pogingen. Probeer het later opnieuw."
    return "Die code klopt niet. Vul de code in die je app nu toont."


async def _elevate_content(request: Request, terug: str, code_fout: str = "") -> HTMLResponse:
    """The content of the popup behind the menu item."""
    email = str((get_current_user(request) or {}).get("email", "")).lower()
    status = await UserOtpService().status(email)
    return HTMLResponse(
        content=render_fragment(
            request,
            template="bg/_verhoogde-rechten.html.j2",
            context={
                "request": request,
                "geregistreerd": status.registered,
                "terug": terug,
                "code_fout": code_fout,
                "venster_minuten": int(STEP_UP_WINDOW.total_seconds() // 60),
                "actief_tot": step_up_until(request),
            },
        )
    )


@step_up_router.get(ELEVATE_PATH, response_model=None)
@action(Action.STEP_UP_VERIFY)
@requires_sso
async def elevate_form(request: Request, terug: str = "/") -> Response:
    """The popup content; a plain visit lands on the page that opens the popup."""
    bestemming = local_redirect_path(terug)
    if not request.headers.get("HX-Request"):
        return RedirectResponse(url=elevate_url(bestemming), status_code=303)
    return await _elevate_content(request, bestemming)


@step_up_router.post(ELEVATE_PATH, response_model=None)
@action(Action.STEP_UP_VERIFY)
@requires_sso
async def elevate(request: Request) -> Response:
    """Activate the step-up from the menu, without a handling waiting behind it."""
    form = await request.form()
    terug = local_redirect_path(str(form.get("terug", "/")))
    email = str((get_current_user(request) or {}).get("email", "")).lower()

    uitkomst = await UserOtpService().verify(email, str(form.get("otp", "")).strip())
    if uitkomst is not OtpVerification.OK:
        logger.warning("Verhoogde rechten geweigerd voor %s: uitkomst %s", email, uitkomst)
        await record_step_up_attempt(email, allowed=False, cause=uitkomst.value, origin=client_ip(request))
        return await _elevate_content(request, terug, code_fout=_code_error(uitkomst))

    record_step_up(request)
    logger.info("Verhoogde rechten geactiveerd door %s", email)
    await record_step_up_attempt(email, allowed=True, cause=None, origin=client_ip(request))
    return Response(headers={"HX-Redirect": terug})


@step_up_router.post("/step-up/end", response_model=None)
@action(Action.STEP_UP_VERIFY)
@requires_sso
async def end_elevation(request: Request) -> Response:
    """End the step-up before its window runs out."""
    clear_step_up(request)
    logger.info("Verhoogde rechten beëindigd door %s", (get_current_user(request) or {}).get("email", ""))
    return Response(headers={"HX-Redirect": "/dashboard"})


@step_up_router.post("/step-up", response_class=HTMLResponse)
@action(Action.STEP_UP_VERIFY)
@requires_sso
async def step_up(request: Request) -> HTMLResponse:
    """Toets de code en voer daarna de handeling uit die erom vroeg.

    Alleen een code uit de app: de factor uit RC-329 kent geen herstelcodes, en wie zijn
    app kwijt is laat een beheerder de factor resetten.
    """
    form = await request.form()
    vervolg = str(form.get("vervolg", ""))
    # De handeling komt uit het formulier en gaat naar de log: een regeleinde erin zou
    # een eigen logregel maken en zo een geslaagde step-up van een ander kunnen forgen.
    handeling = sanitize_for_log(str(form.get("handeling", "deze handeling uit te voeren")))
    vervolg_token = str(form.get("vervolg_token", "")) or None
    vervolg_velden = lees_vervolg_velden(str(form.get("vervolg_velden", "")))
    # Alleen GET en POST kent de herhaling; alles anders leest als de gewone POST.
    vervolg_methode = "GET" if str(form.get("vervolg_methode", "")).upper() == "GET" else "POST"
    code = str(form.get("otp", "")).strip()
    email = str((get_current_user(request) or {}).get("email", "")).lower()

    service = UserOtpService()
    uitkomst = await service.verify(email, code)
    if uitkomst is not OtpVerification.OK:
        logger.warning("Step-up geweigerd voor %s, om %s: uitkomst %s", email, handeling, uitkomst)
        await record_step_up_attempt(email, allowed=False, cause=uitkomst.value, origin=client_ip(request))
        return HTMLResponse(
            content=await render_step_up_dialog(
                request,
                vervolg=vervolg,
                handeling=handeling,
                code_fout=_code_error(uitkomst),
                vervolg_token=vervolg_token,
                vervolg_velden=vervolg_velden,
                methode=vervolg_methode,
            )
        )

    record_step_up(request)
    logger.info("Step-up geslaagd voor %s, om %s", email, handeling)
    await record_step_up_attempt(email, allowed=True, cause=None, origin=client_ip(request))
    return HTMLResponse(
        content=render_step_up_repeat(
            request,
            vervolg=vervolg,
            vervolg_token=vervolg_token,
            vervolg_velden=vervolg_velden,
            methode=vervolg_methode,
        )
    )
