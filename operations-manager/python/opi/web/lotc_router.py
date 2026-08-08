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
from opi.web.menu import get_menu_items

router = APIRouter(prefix="/lotc", tags=["lotc"])


def _context(request: Request, **extra: object) -> dict[str, object]:
    """Basiscontext voor een LOTC-pagina.

    Dezelfde menu-items als de roos-schil: opi/web/menu.py blijft de ene bron. De
    omzetting gaat over hoe een menu getoond wordt, niet over wat erin staat.
    """
    user = request.session.get("user") if hasattr(request, "session") else None
    return {"request": request, "menu_items": get_menu_items(user), **extra}


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def lotc_index(request: Request) -> HTMLResponse:
    """De schil zelf, zonder pagina-inhoud.

    Dit is de kleinste toets op de hele gereedschapsketen: componenttags, het
    gekozen design system, en de statische bestanden onder /static/lotc/.
    """
    return templates_lotc.TemplateResponse(request, "base_lotc.html.j2", _context(request))
