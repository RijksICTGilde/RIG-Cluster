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

- **De release gaat voor.** Zolang de omzetting loopt moet de bestaande pagina blijven
  werken. Een schakelaar houdt de oude weg intact tot de nieuwe aantoonbaar beter is.
- **Vergelijken op DEZELFDE gegevens.** Dat is de enige eerlijke toets. Twee pagina's
  naast elkaar met verschillende data vertelt niets; ``?ui=lotc`` op dezelfde route wel.
- **Terugvallen kost niets.** Gaat er iets mis in productie, dan is het weghalen van een
  querystring genoeg - geen terugdraaien, geen tweede deploy.

Zodra een pagina af is, verdwijnt de keuze: dan noemt de route alleen nog het
LOTC-sjabloon, en uiteindelijk verdwijnt deze module met de laatste pagina.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.responses import Response

#: De querystring die de LOTC-weergave kiest: ``?ui=lotc``.
QUERY_PARAM = "ui"
LOTC_VALUE = "lotc"


def wants_lotc(request: Request) -> bool:
    """Of dit verzoek de LOTC-weergave wil.

    Bewust uit de URL en niet uit een instelling of een cookie: een instelling geldt voor
    iedereen tegelijk, en een cookie maakt onzichtbaar welke versie iemand ziet. Met een
    querystring is het zichtbaar in de adresbalk, deelbaar in een melding, en weg zodra je
    hem weglaat.
    """
    return request.query_params.get(QUERY_PARAM) == LOTC_VALUE


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

    De import van de LOTC-omgeving staat binnenin en niet bovenaan: Lord of the Components
    zit in een dependency-group en niet in de runtime-dependencies, dus in de release-image
    bestaat het pakket niet. Een import bovenaan zou die image laten crashen op een module
    die er niet hoort te zijn.
    """
    if wants_lotc(request):
        from opi.core.templates_lotc import templates_lotc

        return templates_lotc.TemplateResponse(request, lotc, context)

    from opi.core.templates import setup_templates

    return setup_templates().TemplateResponse(request, roos, context)


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
                "help": definition.description,
                # Welke projecten een dienst afnemen weet deze route niet; dat vergt de
                # projectenlijst en die haalt hij niet op. Liever leeg dan verzonnen.
                "used_by": [],
            }
        )

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
