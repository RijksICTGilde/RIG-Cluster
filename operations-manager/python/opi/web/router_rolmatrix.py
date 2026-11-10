"""The role matrix: who may do what, straight from :mod:`opi.core.authz.rules`.

The page keeps no list of its own, so a missing row means the rule is missing, not that the
page lags behind. It is open to everyone who is signed in: it shows rules, no data of any
project.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from opi.core.authz.actions import Action, action
from opi.core.authz.matrix import columns, other_rules, sections
from opi.http.auth.auth_decorators import get_current_user, requires_sso
from opi.http.render.lotc_switch import render
from opi.http.render.menu import get_menu_items
from opi.http.render.navigation_lotc import get_navigation

rolmatrix_router = APIRouter(prefix="/rollen", tags=["rolmatrix"])


@rolmatrix_router.get("", response_class=HTMLResponse)
@action(Action.PORTAL_READ)
@requires_sso
async def rolmatrix(request: Request) -> Response:
    """The matrix: a column per role, a row per rule and scope."""
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
