"""De verse step-up in het portaal (RC-332).

Een handeling die productie raakt wisselt zichzelf in voor een dialoog met een OTP-veld.
Die dialoog post hier; bij een geldige code staat de step-up in de sessie en voert de
dialoog de oorspronkelijke handeling alsnog uit.

De route valt onder de centrale CSRF-bescherming: hij staat niet onder ``/api/``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from opi.api.router import sanitize_for_log
from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.step_up import record_step_up
from opi.services.user_otp_service import OtpVerification, UserOtpService
from opi.web.step_up_dialog import render_step_up_dialog, render_step_up_repeat

logger = logging.getLogger(__name__)

step_up_router = APIRouter(tags=["step-up"])


@step_up_router.post("/step-up", response_class=HTMLResponse)
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
    code = str(form.get("code", "")).strip()
    email = str((get_current_user(request) or {}).get("email", "")).lower()

    service = UserOtpService()
    uitkomst = await service.verify(email, code)
    if uitkomst is not OtpVerification.OK:
        logger.warning("Step-up geweigerd voor %s, om %s: uitkomst %s", email, handeling, uitkomst)
        fout = (
            "Je factor is tijdelijk geblokkeerd na te veel mislukte pogingen. Probeer het later opnieuw."
            if uitkomst is OtpVerification.LOCKED
            else "Die code klopt niet. Vul de code in die je app nu toont."
        )
        return HTMLResponse(
            content=await render_step_up_dialog(
                request, vervolg=vervolg, handeling=handeling, fout=fout, vervolg_token=vervolg_token
            )
        )

    record_step_up(request)
    logger.info("Step-up geslaagd voor %s, om %s", email, handeling)
    return HTMLResponse(content=render_step_up_repeat(request, vervolg=vervolg, vervolg_token=vervolg_token))
