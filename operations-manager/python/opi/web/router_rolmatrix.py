"""De rolmatrix (/rollen): wie mag wat, uit het regelregister.

De pagina rekent niets uit en houdt geen eigen lijst bij: hij toont wat
:mod:`opi.core.authz.rules` declareert. Een regel die daar bijkomt staat hier meteen, en
een rij die hier mist betekent dat de regel ontbreekt, niet dat de pagina achterloopt.

Voor iedereen die ingelogd is, zoals /platform: wie een rol krijgt of uitdeelt, moet kunnen
lezen wat die rol opent. De matrix toont regels, geen gegevens van een project.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.authz.actions import Action, action
from opi.core.authz.matrix import columns, other_rules, sections
from opi.web.lotc_switch import render
from opi.web.menu import get_menu_items
from opi.web.navigation_lotc import get_navigation

rolmatrix_router = APIRouter(prefix="/rollen", tags=["rolmatrix"])


@rolmatrix_router.get("", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def rolmatrix(request: Request) -> Response:
    """De matrix, met een kolom per rol en een rij per regel en scope."""
    user = get_current_user(request)

    return render(
        request,
        template="bg/admin-rolmatrix.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "kolommen": columns(),
            "secties": sections(),
            "andere_regels": other_rules(),
            "navigation": get_navigation(user, current_path="/rollen"),
        },
    )
