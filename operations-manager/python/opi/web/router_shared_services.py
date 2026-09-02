"""De beheerpagina met de toestand van de gedeelde diensten (/admin/diensten).

De pagina zelf doet GEEN metingen: hij zet de kaders neer en haalt de twee blokken
daarna lui op, elk met een eigen verzoek. Zo staat de pagina er meteen, houdt een trage
of onbereikbare metriekbron hem niet op, en kost een kapot blok alleen dat blok. Dezelfde
aanpak als het dashboard en de projectdetailpagina.

Wie hier mag komen: alleen een platformbeheerder. Deze pagina toont gegevens over ALLE
projecten heen, dus ``require_platform_admin`` staat op de pagina en op allebei de
fragmenten - een fragment is een gewone URL, en een grendel op alleen de pagina laat de
gegevens er via het fragment nog steeds uit.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from opi.core.auth_decorators import require_platform_admin, requires_sso
from opi.services.gedeelde_diensten import (
    DREMPELS,
    ONGEMETEN_DIENSTEN,
    haal_databases,
    haal_keycloak,
    haal_opslag,
    haal_resources,
)
from opi.web.lotc_switch import build_lotc_admin, render
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

shared_services_router = APIRouter(prefix="/admin/diensten", tags=["gedeelde-diensten"])


@shared_services_router.get("", response_class=HTMLResponse)
@requires_sso
async def gedeelde_diensten_overzicht(request: Request) -> Response:
    """Het overzicht: de kaders, de drempels en wat er niet gemeten wordt."""
    user = require_platform_admin(request)

    return render(
        request,
        template="bg/admin-gedeelde-diensten.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "drempels": list(DREMPELS.values()),
            "ongemeten": ONGEMETEN_DIENSTEN,
            **build_lotc_admin(user=user, current_path="/admin/diensten"),
        },
    )


@shared_services_router.get("/resources", response_class=HTMLResponse)
@requires_sso
async def gedeelde_diensten_resources(request: Request) -> Response:
    """Het resourceblok, apart opgehaald."""
    require_platform_admin(request)

    blok = await haal_resources()
    return render(
        request,
        template="bg/_gedeelde-diensten-resources.html.j2",
        context={"request": request, "blok": blok},
    )


@shared_services_router.get("/opslag", response_class=HTMLResponse)
@requires_sso
async def gedeelde_diensten_opslag(request: Request) -> Response:
    """Het opslagblok, apart opgehaald."""
    require_platform_admin(request)

    blok = await haal_opslag()
    return render(
        request,
        template="bg/_gedeelde-diensten-opslag.html.j2",
        context={"request": request, "blok": blok},
    )


@shared_services_router.get("/databases", response_class=HTMLResponse)
@requires_sso
async def gedeelde_diensten_databases(request: Request) -> Response:
    """Het databaseblok, apart opgehaald."""
    require_platform_admin(request)

    blok = await haal_databases()
    return render(
        request,
        template="bg/_gedeelde-diensten-databases.html.j2",
        context={"request": request, "blok": blok},
    )


@shared_services_router.get("/keycloak", response_class=HTMLResponse)
@requires_sso
async def gedeelde_diensten_keycloak(request: Request) -> Response:
    """Het Keycloak-blok, apart opgehaald."""
    require_platform_admin(request)

    blok = await haal_keycloak()
    return render(
        request,
        template="bg/_gedeelde-diensten-keycloak.html.j2",
        context={"request": request, "blok": blok},
    )
