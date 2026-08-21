"""Routes van de LOTC-bouwlijn, naast de bestaande pagina's.

Elke omgezette pagina is bereikbaar op ``/lotc/<pad>`` terwijl het origineel op
``<pad>`` blijft staan. Dat is bewust: zolang de omzetting loopt is de enige
bruikbare toets een vergelijking tussen de twee, en die is alleen te maken als ze
tegelijk bestaan. De screenshottests draaien op precies dit paar.

Deze router is PUBLIEK. Hij wordt onvoorwaardelijk geregistreerd (opi/server.py) en
lord-of-the-components is sinds RC-67 een gewone runtime-dependency, dus hij zit ook in
de release-image; geen enkele route hier draagt ``requires_sso``. Dat is voor een
proefopstelling ook de bedoeling - maar het betekent dat wat ``page_data()`` aanlevert
op straat ligt. Alleen zichtbaar verzonnen waarden dus, ook voor de beheerderspagina's:
grenswaarden, of de lijst van welke platformdiensten niet gemeten worden, horen achter
``require_platform_admin`` en niet hier.
"""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from opi.core.templates_lotc import TEMPLATES_LOTC_DIR, templates_lotc
from opi.forms.widgets.lotc import LOTCWidgetAdapter
from opi.web.lotc_fixtures import page_data
from opi.web.lotc_form_preview import EXAMPLE_FIELDS
from opi.web.navigation_lotc import get_navigation

router = APIRouter(prefix="/lotc", tags=["lotc"])

#: De gebruiker die de proefopstelling toont als er niemand is ingelogd.
PREVIEW_USER = {"email": "beheerder@voorbeeld.nl", "name": "Voorbeeldbeheerder"}


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
    # Zonder gebruiker toont het menu alleen "Inloggen", en dan is de hulplinkbalk in de
    # header zo goed als leeg. Voor een proefopstelling die bedoeld is om de VORMGEVING
    # te beoordelen is dat de minst leerzame stand, dus valt hij terug op een
    # voorbeeldgebruiker. Zichtbaar een voorbeeld, net als de projectbestanden.
    user = user or PREVIEW_USER
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


@router.get("/formulier", response_class=HTMLResponse, include_in_schema=False)
async def lotc_form_preview(request: Request) -> HTMLResponse:
    """De omgezette formulierlaag, gerenderd uit voorbeeldvelden.

    De wizard heeft een echt project nodig, dus zonder deze route zou de laag die het
    zwaarst is omgezet ook de enige zijn die niemand kan bekijken.
    """
    adapter = LOTCWidgetAdapter()
    rendered_fields = [adapter.render_field(field) for field in EXAMPLE_FIELDS]
    return templates_lotc.TemplateResponse(
        request,
        "form-preview.html.j2",
        _context(request, rendered_fields=rendered_fields),
    )


# Voorbeelddata voor het herontworpen dashboard. Uit de voorbeeldprojecten, zodat het
# beeld klopt met wat een echte instantie zou tonen en er geen tweede verzonnen
# werkelijkheid naast staat.
_ACTIVITY = [
    {
        "icon": "plus",
        "actor": "Voorbeeldbeheerder",
        "action": "project aangemaakt",
        "resource": "voorbeeld-klein",
        "at": "vandaag 09:12",
    },
    {
        "icon": "arrow-up-arrow-down",
        "actor": "Voorbeeldontwikkelaar",
        "action": "deployment uitgerold",
        "resource": "voorbeeld-volledig / productie",
        "at": "gisteren 16:40",
    },
    {
        "icon": "lock-closed",
        "actor": "Voorbeeldbeheerder",
        "action": "sleutel vernieuwd",
        "resource": "voorbeeld-volledig / api-key",
        "at": "gisteren 11:05",
    },
]


def _redesigned_pages() -> dict[str, str]:
    """De herontworpen paginas in bg-vorm, als allowlist opgebouwd bij het starten.

    Zelfde reden als bij de vertaalde paginas: zonder allowlist zou de naam uit de URL
    rechtstreeks een templatepad worden.
    """
    directory = TEMPLATES_LOTC_DIR / "bg"
    return {
        path.name.removesuffix(".html.j2"): f"bg/{path.name}"
        for path in sorted(directory.glob("*.j2"))
        # Bestanden met een liggend streepje ervoor zijn gedeelde patronen en geen
        # pagina; ze hebben geen eigen inhoud en zouden leeg renderen.
        if not path.name.startswith("_")
    }


REDESIGNED_PAGES = _redesigned_pages()


@router.get("/bg/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def lotc_redesigned_page(request: Request, slug: str) -> HTMLResponse:
    """Een pagina zoals bg.rijks.app hem zou opbouwen.

    Naast de vertaalde versie op /lotc/pagina/<naam>. Het verschil tussen die twee is het
    verschil tussen een omzetting en een herontwerp, en dat hoort te zien te zijn.
    """
    template_name = REDESIGNED_PAGES.get(slug)
    if template_name is None:
        raise HTTPException(status_code=404, detail=f"onbekende pagina: {slug}")

    data = page_data(slug)

    # Het actieve tabblad komt uit de URL, want de tabs zijn echte links. Een onbekende
    # naam valt terug op de eerste in plaats van een fout te geven: een verkeerd
    # gedeelde link hoort de pagina te tonen, niet stuk te gaan.
    # Filteren op de dienstenpagina. Zelfde regel als bij de tabs: de keuze staat in de
    # URL, dus hij is deelbaar en werkt zonder JavaScript.
    if "service_filters" in data:
        chosen = request.query_params.get("kind", "")
        data["service_filter"] = chosen
        if chosen == "system":
            data["services"] = [service for service in data["services"] if service["kind_label"]]
        elif chosen == "user":
            data["services"] = [service for service in data["services"] if not service["kind_label"]]

    tabs = data.get("tabs")
    if tabs:
        requested = request.query_params.get("tab", "")
        data["active_tab"] = requested if requested in tabs else next(iter(tabs))
        # De ECHTE pagina geeft elk tabblad een eigen pad (/projects/<naam>/deployments).
        # Deze proefopstelling heeft geen project en dus geen zulke paden; hier blijft het
        # ?tab= op deze ene demo-URL. Het sjabloon leest in beide gevallen ``tab.url``.
        data["tabs"] = {sleutel: {**tab, "url": f"/lotc/bg/{slug}?tab={sleutel}"} for sleutel, tab in tabs.items()}

    return templates_lotc.TemplateResponse(request, template_name, _context(request, **data))
