"""The platform actions page: for now only the outage mode of SSO Rijk.

Reading and switching both sit behind ``require_platform_admin``, so a platform administrator
with a fresh step-up. The page reads the state out of Keycloak and holds no copy of its own,
and the switch can only ever set one of the two flows the blueprint created.
"""

from __future__ import annotations

import logging
from urllib.parse import quote_plus

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from keycloak.exceptions import KeycloakError
from starlette.responses import Response

from opi.core.authz.actions import Action, action
from opi.http.auth.auth_decorators import require_platform_admin, requires_sso, requires_step_up
from opi.http.auth.step_up_conditions import always_requires_step_up
from opi.http.render.menu import get_menu_items
from opi.services.sso_outage import read_status, set_outage
from opi.services.step_up import StepUpAction
from opi.utils.csrf import ensure_csrf_token

logger = logging.getLogger(__name__)

platform_admin_router = APIRouter(prefix="/admin/platform", tags=["platform-admin"])

PATH = "/admin/platform"


@platform_admin_router.get("", response_class=HTMLResponse)
@action(Action.PLATFORM_SSO_OUTAGE)
@requires_sso
async def platform_actions(request: Request) -> Response:
    """The platform actions, with the real state of the platform realm."""
    user = require_platform_admin(request)

    from opi.http.render.lotc_switch import build_lotc_admin, render

    try:
        status = await read_status()
    except KeycloakError as e:
        logger.error(f"Could not read the browser flow of the platform realm: {e}")
        raise HTTPException(
            status_code=503,
            detail="De stand van de inlogweg is nu niet op te vragen. Probeer het over een minuut opnieuw.",
        ) from e

    return render(
        request,
        template="bg/admin-platform.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "sso_status": status,
            "csrf_token": ensure_csrf_token(request),
            "success_message": request.query_params.get("success"),
            **build_lotc_admin(user=user, current_path=PATH),
        },
    )


@platform_admin_router.post("/sso-storing", response_model=None)
@action(Action.PLATFORM_SSO_OUTAGE, audit_fields=("storingsstand",))
@requires_sso
@requires_step_up(always_requires_step_up, action=StepUpAction.SSO_OUTAGE_SET, fields=("storingsstand",))
async def set_sso_outage(request: Request) -> Response:
    """Switch the outage mode on or off. The audit row carries which way it went."""
    admin = require_platform_admin(request)

    form = await request.form()
    wanted = str(form.get("storingsstand", "")).strip()
    if wanted not in ("aan", "uit"):
        raise HTTPException(status_code=400, detail="Onbekende stand")

    try:
        status = await set_outage(wanted == "aan")
    except KeycloakError as e:
        logger.error(f"Could not set the browser flow of the platform realm: {e}")
        raise HTTPException(
            status_code=503,
            detail="De inlogweg kon niet worden omgezet. Probeer het over een minuut opnieuw.",
        ) from e

    logger.warning(
        "Platform administrator %s switched the SSO Rijk outage mode %s (browser flow '%s')",
        admin.get("email", "unknown"),
        wanted,
        status.browser_flow,
    )

    message = "Storingsstand aan: inloggen via e-mail kan nu" if status.outage_active else "Storingsstand uit"
    url = f"{PATH}?success={quote_plus(message)}"
    if request.headers.get("HX-Request"):
        return HTMLResponse(content="", headers={"HX-Redirect": url})
    return RedirectResponse(url=url, status_code=303)
