"""De schakelaar waarmee een ECHTE route zijn pagina door LOTC laat renderen.

Tot nu toe stond de bouwlijn los: eigen routes onder ``/lotc/``, gevoed met
voorbeeldprojecten. Dat was goed om de vorm te kiezen, maar het is niet het doel. Het
doel is dat ZAD zelf op LOTC draait.

Deze module is de eerste stap daarheen, en hij is met opzet klein: een bestaande route
bouwt zijn gegevens op zoals altijd, en kiest daarna welk sjabloon ze rendert. Dezelfde
route, dezelfde gegevens, dezelfde rechten - alleen een andere weergave.

    return render(request, roos="services-overview.html.j2", lotc="bg/services.html.j2",
                  context={...})

Waarom een schakelaar en niet gewoon omzetten:

- **Pagina voor pagina.** De omzetting gaat per pagina; een schakelaar houdt de oude weg
  intact tot de nieuwe aantoonbaar beter is, zodat de omzetting niet in een keer hoeft.
- **Vergelijken op DEZELFDE gegevens.** Dat is de enige eerlijke toets. Twee pagina's
  naast elkaar met verschillende data vertelt niets; ``?ui=lotc`` op dezelfde route wel.
- **Terugvallen kost niets.** Gaat er iets mis in productie, dan is het weghalen van een
  querystring genoeg - geen terugdraaien, geen tweede deploy.

Zodra een pagina af is, verdwijnt de keuze: dan noemt de route alleen nog het
LOTC-sjabloon, en uiteindelijk verdwijnt deze module met de laatste pagina.
"""

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)

#: Het koekje waarin de keuze bewaard blijft.
COOKIE_NAME = "zad_layout"

#: De waarden die het koekje kan hebben. Zonder koekje geldt de standaard, en die is de
#: nieuwe vormgeving: we zijn aan het overgaan, niet aan het uitproberen.
LAYOUT_LOTC = "nldd"
LAYOUT_ROOS = "roos"
DEFAULT_LAYOUT = LAYOUT_LOTC

#: De querystring blijft bestaan om de keuze te ZETTEN (``?layout=roos``). Handig om in
#: een melding een link mee te sturen, en om eenmalig te vergelijken.
QUERY_PARAM = "layout"


def chosen_layout(request: Request) -> str:
    """Welke vormgeving dit verzoek krijgt.

    Volgorde: de querystring wint van het koekje, en het koekje van de standaard. Zo kun
    je met een link laten zien wat je bedoelt zonder de voorkeur van de ander te
    overschrijven; die komt pas vast te staan als hij zelf wisselt.
    """
    requested = request.query_params.get(QUERY_PARAM)
    if requested in (LAYOUT_LOTC, LAYOUT_ROOS):
        return requested

    stored = request.cookies.get(COOKIE_NAME)
    if stored in (LAYOUT_LOTC, LAYOUT_ROOS):
        return stored

    return DEFAULT_LAYOUT


def wants_lotc(request: Request) -> bool:
    """Of dit verzoek de nieuwe vormgeving krijgt."""
    return chosen_layout(request) == LAYOUT_LOTC


def render(
    request: Request,
    *,
    roos: str,
    lotc: str,
    context: dict[str, Any],
) -> Response:
    """Render ``context`` met het LOTC-sjabloon als daarom gevraagd is, anders met roos.

    Args:
        request: het binnenkomende verzoek; bepaalt de keuze.
        roos: de bestaande template, in ``opi/templates/``.
        lotc: de LOTC-template, in ``opi/templates_lotc/``.
        context: de gegevens, identiek voor beide.

    De import staat binnenin en niet bovenaan om een kringloop te vermijden: de
    LOTC-omgeving leent zijn filters en globals van de roos-omgeving, en die wordt door de
    routes geimporteerd die deze module gebruiken.
    """
    if wants_lotc(request):
        from opi.core.templates_lotc import templates_lotc

        return remember_layout(request, templates_lotc.TemplateResponse(request, lotc, context))

    from opi.core.templates import setup_templates

    return remember_layout(request, setup_templates().TemplateResponse(request, roos, context))


# Hoe een dienst gebonden is, in gewone taal. De registry noemt dit "binding", en dat
# zegt een gebruiker niets.
BINDING_LABELS = {
    "component": "per component",
    "deployment": "per deployment",
    "project": "per project",
}


def build_lotc_services(
    request: Request,
    services_info: list[dict[str, Any]],
    user: dict[str, Any] | None,
) -> dict[str, Any]:
    """Zet de ECHTE servicegegevens om naar wat de hertekende dienstenpagina verwacht.

    Dit is de kern van "echt omzetten" en niet "een voorbeeld maken": de route bouwt zijn
    gegevens op zoals altijd, uit de registry, en hier worden ze alleen in de vorm gezet
    die het nieuwe sjabloon leest. Er komt geen tweede bron bij.

    Levert niets op als de LOTC-weergave niet gevraagd is; dan is dit werk voor niets en
    hoeft de bestaande pagina er niet mee lastiggevallen te worden.
    """
    if not wants_lotc(request):
        return {}

    from opi.web.navigation_lotc import get_navigation, to_nldd_icon

    services: list[dict[str, Any]] = []
    for entry in services_info:
        definition = entry["definition"]
        if getattr(definition, "hidden", False):
            continue

        binding = getattr(definition.binding, "value", str(definition.binding))
        is_platform = definition.kind.value == "system"

        chips = [BINDING_LABELS.get(binding, binding)]
        if entry["variables"]:
            chips.append(f"{len(entry['variables'])} variabelen")
        if getattr(definition, "requires", None):
            chips.append(f"vereist {len(definition.requires)}")

        services.append(
            {
                "name": entry["service"].value,
                "label": definition.name,
                "summary": definition.description,
                "icon": to_nldd_icon(definition.icon),
                "color": definition.color,
                "chips": chips,
                "kind_label": "altijd aan" if is_platform else "",
                "kind_type": "info",
                "help_template": getattr(definition, "help_template", None),
                # Welke projecten een dienst afnemen weet deze route niet; dat vergt de
                # projectenlijst en die haalt hij niet op. Liever leeg dan verzonnen.
                "used_by": [],
            }
        )

    # De uitgebreide hulptekst van een dienst, als erom gevraagd is. Server-side en niet
    # in een tooltip: elke dienst heeft een eigen help-template met een echt verhaal, en
    # dat wil je LEZEN. Een tooltip verschijnt bij zweven en verdwijnt weer; mensen
    # klikken. Zo is de uitleg bovendien een deelbare URL en werkt hij zonder JavaScript.
    gevraagde_hulp = request.query_params.get("help", "")
    for service in services:
        service["help_open"] = service["name"] == gevraagde_hulp
        service["help_html"] = _render_help(request, service["help_template"]) if service["help_open"] else ""

    chosen = request.query_params.get("kind", "")
    if chosen == "system":
        shown = [service for service in services if service["kind_label"]]
    elif chosen == "user":
        shown = [service for service in services if not service["kind_label"]]
    else:
        shown = services

    return {
        "navigation": get_navigation(user, current_path="/services"),
        "services": shown,
        "service_filter": chosen,
        "service_filters": [
            ("", "Alle", len(services)),
            ("user", "Zelf te kiezen", sum(1 for s in services if not s["kind_label"])),
            ("system", "Altijd aan", sum(1 for s in services if s["kind_label"])),
        ],
    }


def build_lotc_dashboard(request: Request, *, user: dict[str, Any] | None, **_ongebruikt: Any) -> dict[str, Any]:
    """Wat het dashboard extra nodig heeft: alleen de navigatie.

    De route levert alle gegevens al - kerncijfers, gezondheid, metrics, projecten - en
    het sjabloon leest precies die sleutels. Ze hier omvormen zou een tweede vorm van
    dezelfde gegevens opleveren, en dan gaat de nieuwe pagina iets anders tonen dan de
    bestaande zodra er een veld bijkomt.
    """
    if not wants_lotc(request):
        return {}

    from opi.web.navigation_lotc import get_navigation

    return {"navigation": get_navigation(user, current_path="/dashboard")}


def build_lotc_admin(request: Request, *, user: dict[str, Any] | None, current_path: str) -> dict[str, Any]:
    """Wat een beheerpagina extra nodig heeft: alleen de navigatie.

    Een functie voor alle vier de beheerpagina's (gebruikers, het gebruikersformulier,
    domeinbeheer en gebruik & kosten), want ze hebben alle vier hetzelfde nodig. Hun
    routes leveren de rest al in de vorm die de sjablonen lezen; die hier omvormen zou
    een tweede vorm van dezelfde gegevens opleveren, en dan gaat de nieuwe pagina iets
    anders tonen dan de bestaande zodra er een veld bijkomt.

    ``current_path`` bepaalt welk item in de zijkolom actief is en verschilt dus wel per
    pagina.
    """
    if not wants_lotc(request):
        return {}

    from opi.web.navigation_lotc import get_navigation

    return {"navigation": get_navigation(user, current_path=current_path)}


def build_lotc_projects(
    request: Request,
    *,
    user: dict[str, Any] | None,
    projects: list[dict[str, Any]],
) -> dict[str, Any]:
    """De ECHTE projectenlijst, in de vorm die de hertekende catalogus leest."""
    if not wants_lotc(request):
        return {}

    from opi.web.navigation_lotc import get_navigation

    # Tellen op de LIJSTEN die deze route levert, niet op een deployment_count-sleutel.
    # Die bestaat wel op het dashboard maar niet hier, en het gevolg was een overzicht
    # waarin alles nul was terwijl er projecten met deployments stonden.
    return {
        "navigation": get_navigation(user, current_path="/projects"),
        "projects": [
            {
                "name": project["name"],
                "display_name": project.get("display_name") or project["name"],
                "description": project.get("description", ""),
                "deployment_count": len(project.get("deployments") or []),
            }
            for project in projects
        ],
    }


#: De tabbladen van de projectpagina. Dezelfde drie als de bestaande pagina (Project,
#: Deployments, Taken), met Metrics erbij: het resourcegebruik zat daar tussen de
#: tekstblokken terwijl het het enige is dat je periodiek komt bekijken.
PROJECT_TABS = {
    "project": {"label": "Project"},
    "deployments": {"label": "Deployments"},
    "metrics": {"label": "Metrics"},
    "taken": {"label": "Taken"},
}


def build_lotc_project_details(
    request: Request,
    *,
    user: dict[str, Any] | None,
    project: dict[str, Any],
) -> dict[str, Any]:
    """De ECHTE projectgegevens, in de vorm die de pagina met tabs leest.

    Er wordt niets omgerekend: ``project`` heeft al de vorm die de route opbouwt, en het
    sjabloon leest precies die sleutels. Wat hier gebeurt is de navigatie en het actieve
    tabblad bepalen.

    Het resourcegebruik zit hier NIET in. De bestaande pagina laadt dat apart met htmx,
    zodat een trage Prometheus de pagina niet ophoudt, en dat blijft zo - het fragment
    kent zijn eigen LOTC-weergave.
    """
    if not wants_lotc(request):
        return {}

    from opi.web.navigation_lotc import get_navigation

    requested = request.query_params.get("tab", "")
    return {
        "navigation": get_navigation(user, current_path="/projects"),
        "tabs": PROJECT_TABS,
        "active_tab": requested if requested in PROJECT_TABS else next(iter(PROJECT_TABS)),
        "project": project,
    }


def remember_layout(request: Request, response: Response) -> Response:
    """Bewaar een expliciete keuze uit de querystring in het koekje.

    Alleen bij een EXPLICIETE keuze: wie de standaard krijgt, krijgt geen koekje. Anders
    zou iedereen die een keer een pagina opent zijn voorkeur vastzetten op wat toevallig
    de standaard was, en dan verandert een latere wijziging van die standaard niets meer
    voor hem.
    """
    requested = request.query_params.get(QUERY_PARAM)
    if requested in (LAYOUT_LOTC, LAYOUT_ROOS):
        response.set_cookie(
            COOKIE_NAME,
            requested,
            max_age=60 * 60 * 24 * 365,
            httponly=False,
            samesite="lax",
        )
    return response


def _render_help(request: Request, template_name: str | None) -> str:
    """Render de hulptekst van een dienst tot HTML.

    Met de BESTAANDE templateomgeving: die teksten zijn in roos-componenten geschreven en
    horen daar thuis. Wat eruit komt is gewone HTML, dus hij past net zo goed in een
    LOTC-pagina. Ze omzetten zou een tweede versie van elke hulptekst opleveren, en dat is
    precies het soort verdubbeling dat vroeg of laat uiteenloopt.
    """
    if not template_name:
        return ""

    from opi.core.templates import setup_templates

    path = template_name if "/" in template_name else f"help/{template_name}"
    try:
        template = setup_templates().env.get_template(path)
    except Exception:
        logger.warning("hulptekst niet gevonden: %s", path)
        return ""
    return template.render(request=request)
