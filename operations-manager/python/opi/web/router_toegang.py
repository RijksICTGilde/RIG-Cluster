"""De beheerpagina Toegang (/admin/toegang): waar de platformdiensten staan en hoe je erin
komt.

Wie hier mag komen: alleen een platformbeheerder. Dit is de zwaarste pagina van het portaal
qua inhoud, want hier staan de wachtwoorden van Keycloak, Forgejo en ArgoCD bij elkaar.

EEN PAGINA EN GEEN LUI OPGEHAALDE BLOKKEN, anders dan /admin/diensten. Daar is het lui
ophalen er omdat een trage metriekbron de pagina anders ophoudt; hier zijn het drie
``kubectl get secret``-aanroepen die naast elkaar draaien, en een fragment zou een tweede
URL zijn waar dezelfde wachtwoorden uitkomen. Eén ingang is er één om te bewaken.

GEEN CACHE. De inhoud mag niet in een proxy of in de terugknop-cache van de browser
blijven hangen. ``no-store`` is daarvoor het enige dat telt; ``no-cache`` zou hem nog steeds
laten opslaan.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.responses import Response

from opi.core.auth_decorators import require_platform_admin, requires_sso
from opi.services.platform_toegang import haal_toegang
from opi.web.lotc_switch import build_lotc_admin, render
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)

toegang_router = APIRouter(prefix="/admin/toegang", tags=["toegang"])


@toegang_router.get("", response_class=HTMLResponse)
@requires_sso
async def toegang_overzicht(request: Request) -> Response:
    """De diensten met hun adres, gebruikersnaam en wachtwoord."""
    user = require_platform_admin(request)

    # Een auditregel, want dit is de pagina waar alle platformwachtwoorden samenkomen. Wie
    # hem opende hoort terug te vinden zijn zonder dat je de webserverlogs erbij hoeft te
    # halen.
    logger.info(f"Toegangspagina geopend door {user.get('email') if isinstance(user, dict) else user}")

    diensten = await haal_toegang()

    antwoord = render(
        request,
        template="bg/admin-toegang.html.j2",
        context={
            "request": request,
            "menu_items": get_menu_items(user),
            "diensten": diensten,
            **build_lotc_admin(user=user, current_path="/admin/toegang"),
        },
    )
    antwoord.headers["Cache-Control"] = "no-store"
    return antwoord
