"""Routes van de LOTC-bouwlijn, naast de bestaande pagina's.

Elke omgezette pagina is bereikbaar op ``/lotc/<pad>`` terwijl het origineel op
``<pad>`` blijft staan. Dat is bewust: zolang de omzetting loopt is de enige
bruikbare toets een vergelijking tussen de twee, en die is alleen te maken als ze
tegelijk bestaan. De screenshottests draaien op precies dit paar.

De router wordt alleen geregistreerd als lord-of-the-components geinstalleerd is
(dependency-group "lotc"), dus in de release-image bestaat hij niet.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from opi.core.templates_lotc import templates_lotc
from opi.web.navigation_lotc import get_navigation

router = APIRouter(prefix="/lotc", tags=["lotc"])


def _context(request: Request, **extra: object) -> dict[str, object]:
    """Basiscontext voor een LOTC-pagina.

    Dezelfde menu-items als de roos-schil: opi/web/menu.py blijft de ene bron voor
    welke links er zijn en wie ze mag zien. navigation_lotc herschikt ze alleen naar
    de bg-opzet, zodat de omzetting over weergave gaat en niet over inhoud.
    """
    user = request.session.get("user") if hasattr(request, "session") else None
    return {
        "request": request,
        "navigation": get_navigation(user, current_path=request.url.path.removeprefix("/lotc")),
        **extra,
    }


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def lotc_index(request: Request) -> HTMLResponse:
    """De schil zelf, zonder pagina-inhoud.

    Dit is de kleinste toets op de hele gereedschapsketen: componenttags, het
    gekozen design system, en de statische bestanden onder /static/lotc/.
    """
    return templates_lotc.TemplateResponse(request, "base_lotc.html.j2", _context(request))
