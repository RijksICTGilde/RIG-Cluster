"""The dialog with which the portal asks for a fresh step-up (RC-332).

It lands in the same dialog as the confirmation that called it up: the answer carries
``HX-Retarget`` to the shell of the shared modal, so it does not matter what target the
action itself had.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from fastapi.responses import HTMLResponse

from opi.http.auth.auth_decorators import get_current_user
from opi.services.step_up import STEP_UP_WINDOW
from opi.services.user_otp_service import UserOtpService

if TYPE_CHECKING:
    from starlette.requests import Request

logger = logging.getLogger(__name__)

#: The contents of the shared dialog from ``bg/_modals.html.j2``.
MODAL_TARGET = "#edit-section-inner"


def _fragment(request: Request, template: str, context: dict) -> str:
    from opi.http.render.lotc_switch import render_fragment

    return render_fragment(request, template=template, context={"request": request, **context})


def _vals(follow_token: str | None, follow_fields: dict[str, str] | None) -> str:
    """The body the repeat sends along, as ``hx-vals`` JSON."""
    fields = dict(follow_fields or {})
    if follow_token:
        fields["_wizard_token"] = follow_token
    return json.dumps(fields, sort_keys=True)


def read_follow_fields(raw: str) -> dict[str, str]:
    """The fields the dialog carried along, back out of the form.

    Unreadable reads as no fields: the value comes from the browser, and the receiving route
    validates its own fields itself anyway.
    """
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        logger.warning("Onleesbare vervolgvelden in de step-up, genegeerd")
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


async def render_step_up_dialog(
    request: Request,
    *,
    follow_url: str,
    action_text: str,
    code_error: str = "",
    follow_token: str | None = None,
    follow_fields: dict[str, str] | None = None,
    method: str = "POST",
) -> str:
    """The dialog with the OTP field, or the explanation that there is no factor yet.

    ``follow_token`` is the wizard token the repeat has to send in the BODY, beside the
    query. ``follow_fields`` are the form fields of the action itself, which the dialog
    replaced. See :func:`render_step_up_repeat`.

    ``method`` is the action's method (GET or POST): the repeat after a successful code has
    to use the same method, and a read window is a GET (RC-388).
    """
    email = str((get_current_user(request) or {}).get("email", "")).lower()
    status = await UserOtpService().status(email)
    return _fragment(
        request,
        "bg/_step-up.html.j2",
        {
            "vervolg": follow_url,
            "handeling": action_text,
            "code_fout": code_error,
            "geregistreerd": status.registered,
            "vervolg_token": follow_token or "",
            "vervolg_velden": json.dumps(follow_fields or {}, sort_keys=True),
            "vervolg_methode": method,
            "venster_minuten": int(STEP_UP_WINDOW.total_seconds() // 60),
        },
    )


def render_step_up_repeat(
    request: Request,
    *,
    follow_url: str,
    follow_token: str | None = None,
    follow_fields: dict[str, str] | None = None,
    method: str = "POST",
) -> str:
    """The fragment that performs the action after all, now that the step-up is there.

    A wizard token also goes along as a body value, like the confirm button in
    ``bg/_modal-wizard-review.html.j2``: htmx does not keep a query string on a POST
    everywhere, and the receiving route reads the token out of both. The fields of the
    action itself travel the same way. ``method`` is that of the original action: a read
    window goes again with a GET (RC-388).
    """
    return _fragment(
        request,
        "bg/_step-up-herhalen.html.j2",
        {"vervolg": follow_url, "vals": _vals(follow_token, follow_fields), "methode": method},
    )


def step_up_dialog_response(html: str) -> HTMLResponse:
    """The htmx answer that puts the dialog in the shared modal."""
    return HTMLResponse(content=html, headers={"HX-Retarget": MODAL_TARGET, "HX-Reswap": "innerHTML"})
