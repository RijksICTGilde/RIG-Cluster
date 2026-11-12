"""The step-up in the portal: a fresh OTP code before an action that touches production.

The action swaps itself for a dialog with an OTP field, that dialog posts here, and on a valid
code the step-up is recorded in the session and the dialog carries out the original action.

These routes fall under the central CSRF protection, because they do not sit under ``/api/``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from opi.core.authz.actions import Action, action
from opi.core.logging_redact import sanitize_for_log
from opi.core.request_user import get_current_user
from opi.http.auth.auth_decorators import ELEVATE_PATH, elevate_url, requires_sso
from opi.http.auth.step_up_dialog import read_follow_fields, render_step_up_dialog, render_step_up_repeat
from opi.http.render.lotc_switch import render_fragment
from opi.services.audit import record_step_up_attempt
from opi.services.step_up import STEP_UP_WINDOW, clear_step_up, record_step_up, step_up_until
from opi.services.user_otp_service import OtpVerification, UserOtpService
from opi.utils.client_ip import client_ip
from opi.utils.url_paths import local_redirect_path

logger = logging.getLogger(__name__)

step_up_router = APIRouter(tags=["step-up"])


def _code_error(outcome: OtpVerification) -> str:
    if outcome is OtpVerification.LOCKED:
        return "Je factor is tijdelijk geblokkeerd na te veel mislukte pogingen. Probeer het later opnieuw."
    return "Die code klopt niet. Vul de code in die je app nu toont."


async def _elevate_content(request: Request, terug: str, code_error: str = "") -> HTMLResponse:
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
                "code_fout": code_error,
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
    destination = local_redirect_path(terug)
    if not request.headers.get("HX-Request"):
        return RedirectResponse(url=elevate_url(destination), status_code=303)
    return await _elevate_content(request, destination)


@step_up_router.post(ELEVATE_PATH, response_model=None)
@action(Action.STEP_UP_VERIFY)
@requires_sso
async def elevate(request: Request) -> Response:
    """Activate the step-up from the menu, without a handling waiting behind it."""
    form = await request.form()
    terug = local_redirect_path(str(form.get("terug", "/")))
    email = str((get_current_user(request) or {}).get("email", "")).lower()

    outcome = await UserOtpService().verify(email, str(form.get("otp", "")).strip())
    if outcome is not OtpVerification.OK:
        logger.warning("Verhoogde rechten geweigerd voor %s: uitkomst %s", email, outcome)
        await record_step_up_attempt(email, allowed=False, cause=outcome.value, origin=client_ip(request))
        return await _elevate_content(request, terug, code_error=_code_error(outcome))

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
    """Check the code, then carry out the action that asked for it.

    Only a code from the app: the factor has no recovery codes, so someone who lost their app
    has an admin reset the factor.
    """
    form = await request.form()
    vervolg = str(form.get("vervolg", ""))
    # The action comes from the form and goes into the log, where a line break in it would
    # make a log line of its own and could forge someone else's successful step-up.
    handeling = sanitize_for_log(str(form.get("handeling", "deze handeling uit te voeren")))
    vervolg_token = str(form.get("vervolg_token", "")) or None
    follow_fields = read_follow_fields(str(form.get("vervolg_velden", "")))
    # The repeat knows GET and POST only; anything else reads as the plain POST.
    vervolg_methode = "GET" if str(form.get("vervolg_methode", "")).upper() == "GET" else "POST"
    code = str(form.get("otp", "")).strip()
    email = str((get_current_user(request) or {}).get("email", "")).lower()

    service = UserOtpService()
    outcome = await service.verify(email, code)
    if outcome is not OtpVerification.OK:
        logger.warning("Step-up geweigerd voor %s, om %s: uitkomst %s", email, handeling, outcome)
        await record_step_up_attempt(email, allowed=False, cause=outcome.value, origin=client_ip(request))
        return HTMLResponse(
            content=await render_step_up_dialog(
                request,
                follow_url=vervolg,
                action_text=handeling,
                code_error=_code_error(outcome),
                follow_token=vervolg_token,
                follow_fields=follow_fields,
                method=vervolg_methode,
            )
        )

    record_step_up(request)
    logger.info("Step-up geslaagd voor %s, om %s", email, handeling)
    await record_step_up_attempt(email, allowed=True, cause=None, origin=client_ip(request))
    return HTMLResponse(
        content=render_step_up_repeat(
            request,
            follow_url=vervolg,
            follow_token=vervolg_token,
            follow_fields=follow_fields,
            method=vervolg_methode,
        )
    )
