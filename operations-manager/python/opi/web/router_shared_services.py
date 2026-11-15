"""The admin page with the state of the shared services.

The page itself measures NOTHING: it puts the frames down and fetches each block lazily, in a
request of its own, so a slow or unreachable metrics source costs only its own block.

``require_platform_admin`` sits on the page AND on every fragment: a fragment is a plain URL,
and a guard on the page alone still lets the data out through the fragment. The page shows data
across ALL projects.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from opi.core.authz.actions import Action, action
from opi.http.auth.auth_decorators import require_platform_admin, requires_sso
from opi.http.render.lotc_switch import build_lotc_admin, render
from opi.http.render.menu import get_menu_items
from opi.services.shared_services import (
    THRESHOLDS,
    UNMEASURED_SERVICES,
    fetch_databases,
    fetch_keycloak,
    fetch_resources,
    fetch_storage,
)

logger = logging.getLogger(__name__)

shared_services_router = APIRouter(prefix="/admin/diensten", tags=["gedeelde-diensten"])


@shared_services_router.get("", response_class=HTMLResponse)
@action(Action.PLATFORM_SERVICES)
@requires_sso
async def gedeelde_diensten_overzicht(request: Request) -> Response:
    """The overview: the frames, the thresholds and what is not measured."""
    user = require_platform_admin(request)

    return render(
        request,
        template="bg/admin-gedeelde-diensten.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "drempels": list(THRESHOLDS.values()),
            "ongemeten": UNMEASURED_SERVICES,
            **build_lotc_admin(user=user, current_path="/admin/diensten"),
        },
    )


@shared_services_router.get("/resources", response_class=HTMLResponse)
@action(Action.PLATFORM_SERVICES)
@requires_sso
async def gedeelde_diensten_resources(request: Request) -> Response:
    """The resources block, fetched on its own."""
    require_platform_admin(request)

    block = await fetch_resources()
    return render(
        request,
        template="bg/_gedeelde-diensten-resources.html.j2",
        context={"request": request, "blok": block},
    )


@shared_services_router.get("/opslag", response_class=HTMLResponse)
@action(Action.PLATFORM_SERVICES)
@requires_sso
async def gedeelde_diensten_opslag(request: Request) -> Response:
    """The storage block, fetched on its own."""
    require_platform_admin(request)

    block = await fetch_storage()
    return render(
        request,
        template="bg/_gedeelde-diensten-opslag.html.j2",
        context={"request": request, "blok": block},
    )


@shared_services_router.get("/databases", response_class=HTMLResponse)
@action(Action.PLATFORM_SERVICES)
@requires_sso
async def gedeelde_diensten_databases(request: Request) -> Response:
    """The database block, fetched on its own."""
    require_platform_admin(request)

    block = await fetch_databases()
    return render(
        request,
        template="bg/_gedeelde-diensten-databases.html.j2",
        context={"request": request, "blok": block},
    )


@shared_services_router.get("/keycloak", response_class=HTMLResponse)
@action(Action.PLATFORM_SERVICES)
@requires_sso
async def gedeelde_diensten_keycloak(request: Request) -> Response:
    """The Keycloak block, fetched on its own."""
    require_platform_admin(request)

    block = await fetch_keycloak()
    return render(
        request,
        template="bg/_gedeelde-diensten-keycloak.html.j2",
        context={"request": request, "blok": block},
    )
