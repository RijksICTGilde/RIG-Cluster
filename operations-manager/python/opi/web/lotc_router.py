"""Routes van de LOTC-bouwlijn, naast de bestaande pagina's.

Elke omgezette pagina is bereikbaar op ``/lotc/<pad>`` terwijl het origineel op
``<pad>`` blijft staan. Dat is bewust: zolang de omzetting loopt is de enige
bruikbare toets een vergelijking tussen de twee, en die is alleen te maken als ze
tegelijk bestaan. De screenshottests draaien op precies dit paar.

De router wordt alleen geregistreerd als lord-of-the-components geinstalleerd is
(dependency-group "lotc"), dus in de release-image bestaat hij niet.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.templates_lotc import TEMPLATES_LOTC_DIR, templates_lotc
from opi.web.navigation_lotc import get_navigation

router = APIRouter(prefix="/lotc", tags=["lotc"])


def _previewable_pages() -> dict[str, str]:
    """De omgezette pagina's die zonder paginadata te bekijken zijn.

    Een verrassend groot deel van de applicatie toont vooral inhoud, en die pagina's
    renderen compleet met alleen de navigatie. Precies die zijn bruikbaar om de
    omzetting te BEKIJKEN, en dat is waar deze route voor is.

    De uitkomst is een allowlist, opgebouwd bij het starten. Dat is geen sierlijkheid:
    zonder zou de paginanaam uit de URL rechtstreeks een templatepad worden, en dan
    kan iemand met ../ elk template in de zoekpaden laten renderen.
    """
    pages: dict[str, str] = {}
    for path in sorted(TEMPLATES_LOTC_DIR.rglob("*.j2")):
        name = str(path.relative_to(TEMPLATES_LOTC_DIR))
        if 'extends "base_lotc.html.j2"' not in path.read_text():
            continue
        slug = str(Path(name).with_suffix("")).removesuffix(".html")
        pages[slug] = name
    return pages


PREVIEWABLE_PAGES = _previewable_pages()


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


@router.get("/pagina/{slug:path}", response_class=HTMLResponse, include_in_schema=False)
async def lotc_page(request: Request, slug: str) -> HTMLResponse:
    """Toon een omgezette pagina, om hem naast het origineel te kunnen leggen.

    Alleen pagina's uit de allowlist; een onbekende naam is een 404 en geen poging tot
    laden. Pagina's die wel bestaan maar paginadata nodig hebben, geven een 422 met de
    reden erbij - dat is bruikbare informatie over hoever de omzetting staat, en
    prettiger dan een stacktrace.
    """
    template_name = PREVIEWABLE_PAGES.get(slug)
    if template_name is None:
        raise HTTPException(status_code=404, detail=f"onbekende pagina: {slug}")
    try:
        return templates_lotc.TemplateResponse(request, template_name, _context(request))
    except Exception as error:
        raise HTTPException(
            status_code=422,
            detail=f"{template_name} rendert nog niet zonder paginadata: {error}",
        ) from error
