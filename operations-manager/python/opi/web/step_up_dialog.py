"""De dialoog waarmee het portaal om een verse step-up vraagt (RC-332).

Hij komt in dezelfde dialoog te staan als de bevestiging die hem opriep: het antwoord
draagt ``HX-Retarget`` naar de schil van de gedeelde modal, zodat het niet uitmaakt welk
doel de handeling zelf had.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastapi.responses import HTMLResponse

from opi.core.auth_decorators import get_current_user
from opi.services.user_otp_service import UserOtpService

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

#: De inhoud van de gedeelde dialoog uit ``bg/_modals.html.j2``.
MODAL_TARGET = "#edit-section-inner"


def _fragment(request: Request, template: str, context: dict) -> str:
    from opi.web.lotc_switch import render_fragment

    return render_fragment(request, template=template, context={"request": request, **context})


def _vals(vervolg_token: str | None, vervolg_velden: dict[str, str] | None) -> str:
    """De body die de herhaling meestuurt, als ``hx-vals``-JSON."""
    velden = dict(vervolg_velden or {})
    if vervolg_token:
        velden["_wizard_token"] = vervolg_token
    return json.dumps(velden, sort_keys=True)


def lees_vervolg_velden(ruw: str) -> dict[str, str]:
    """De velden die de dialoog meedroeg, terug uit het formulier.

    Onleesbaar leest als geen velden: de waarde komt uit de browser, en de ontvangende
    route valideert zijn eigen velden toch zelf.
    """
    try:
        gelezen = json.loads(ruw or "{}")
    except json.JSONDecodeError:
        logger.warning("Onleesbare vervolgvelden in de step-up, genegeerd")
        return {}
    if not isinstance(gelezen, dict):
        return {}
    return {str(sleutel): str(waarde) for sleutel, waarde in gelezen.items()}


async def render_step_up_dialog(
    request: Request,
    *,
    vervolg: str,
    handeling: str,
    fout: str = "",
    vervolg_token: str | None = None,
    vervolg_velden: dict[str, str] | None = None,
) -> str:
    """De dialoog met het OTP-veld, of de uitleg dat er nog geen factor is.

    ``vervolg_token`` is het wizard-token dat de herhaling in de BODY moet meesturen,
    naast de query. ``vervolg_velden`` zijn de formuliervelden van de handeling zelf, die
    de dialoog verving. Zie :func:`render_step_up_repeat`.
    """
    email = str((get_current_user(request) or {}).get("email", "")).lower()
    status = await UserOtpService().status(email)
    return _fragment(
        request,
        "bg/_step-up.html.j2",
        {
            "vervolg": vervolg,
            "handeling": handeling,
            "fout": fout,
            "geregistreerd": status.registered,
            "vervolg_token": vervolg_token or "",
            "vervolg_velden": json.dumps(vervolg_velden or {}, sort_keys=True),
        },
    )


def render_step_up_repeat(
    request: Request,
    *,
    vervolg: str,
    vervolg_token: str | None = None,
    vervolg_velden: dict[str, str] | None = None,
) -> str:
    """Het fragment dat de handeling alsnog uitvoert, nu de step-up er is.

    Een wizard-token gaat ook als body-waarde mee, net als de bevestigknop in
    ``bg/_modal-wizard-review.html.j2``: htmx laat een query string op een POST niet
    overal staan, en de ontvangende route leest het token uit beide. De velden van de
    handeling zelf gaan langs dezelfde weg mee.
    """
    return _fragment(
        request,
        "bg/_step-up-herhalen.html.j2",
        {"vervolg": vervolg, "vals": _vals(vervolg_token, vervolg_velden)},
    )


def step_up_dialog_response(html: str) -> HTMLResponse:
    """Het htmx-antwoord dat de dialoog in de gedeelde modal zet."""
    return HTMLResponse(content=html, headers={"HX-Retarget": MODAL_TARGET, "HX-Reswap": "innerHTML"})
