"""Hoe een route zijn pagina rendert, en wat de hertekende pagina's aan gegevens vragen.

Hier stond een SCHAKELAAR. Zolang de omzetting per pagina liep, koos ``?layout=`` (of het
koekje ``zad_layout``) tussen de bestaande pagina en de hertekende, en noemde elke route
allebei de sjablonen:

    return render(request, roos="services-overview.html.j2", lotc="bg/services.html.j2", ...)

Die keuze is er niet meer. Er wordt overgestapt en niet parallel gedraaid: de roos-weg is
weg, dus er valt niets meer te kiezen en een route noemt nog EEN sjabloon. Wat blijft is
het saaie deel dat altijd al nuttig was - de gegevens van een route in de vorm zetten die
de hertekende pagina leest - plus de weergavekeuze licht/donker onderaan.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.responses import Response


def render(request: Request, *, template: str, context: dict[str, Any]) -> Response:
    """Render ``context`` met ``template`` uit ``opi/templates_lotc/``.

    Args:
        request: het binnenkomende verzoek.
        template: de template, in ``opi/templates_lotc/``.
        context: de gegevens.

    De import staat binnenin en niet bovenaan om een kringloop te vermijden: de
    templateomgeving leunt op de formulierlaag, en die wordt door de routes geimporteerd
    die deze module gebruiken.
    """
    from opi.core.templates_lotc import templates_lotc

    return templates_lotc.TemplateResponse(request, template, context)


def render_fragment(request: Request, *, template: str, context: dict[str, Any]) -> str:
    """Render een FRAGMENT als HTML-string.

    De tegenhanger van :func:`render` voor stukken die geen ``TemplateResponse`` worden
    maar een string die de route zelf in een ``HTMLResponse`` zet - de inhoud van de
    gedeelde dialoog, bijvoorbeeld.

    Er gaat NOOIT een tweede slag overheen. De sjablonen hier zijn bestanden, dus hun
    componenttags zijn al bij het compileren vervangen, en de formulier-HTML die erin komt
    is door de formulierlaag al afgerenderd. Een tweede Jinja-render zou de ingevulde
    waarden alsnog als sjabloon uitvoeren; dat is in deze codebase eerder een lek geweest.

    ``request`` wordt niet gebruikt en staat er omdat elke aanroeper hem heeft: het houdt
    deze functie gelijkvormig aan :func:`render`.
    """
    from opi.core.templates_lotc import templates_lotc

    return templates_lotc.env.get_template(template).render(context)


def build_lotc_services(
    services_info: list[dict[str, Any]],
    user: dict[str, Any] | None,
) -> dict[str, Any]:
    """Zet de ECHTE servicegegevens om naar wat de hertekende dienstenpagina verwacht.

    Dit is de kern van "echt omzetten" en niet "een voorbeeld maken": de route bouwt zijn
    gegevens op zoals altijd, uit de registry, en hier worden ze alleen in de vorm gezet
    die het nieuwe sjabloon leest. Er komt geen tweede bron bij.
    """
    from opi.web.navigation_lotc import get_navigation, to_nldd_icon

    # Elke dienst die de route aanlevert komt op de pagina, ook de dienst met
    # ``hidden=True``. Die vlag betekent "niet aanbieden in de WIZARD"
    # (namespace-postgresql-database en namespace-redis worden via de API toegekend); de
    # bestaande overzichtspagina toont ze wel, want ze leveren omgevingsvariabelen die je
    # moet kunnen opzoeken. Hier werden ze overgeslagen, en dat kostte twee van de
    # eenentwintig kaarten - zonder dat iets erover klaagde.
    services: list[dict[str, Any]] = [
        {
            "name": entry["service"].value,
            "label": definition.name,
            "summary": definition.description,
            "icon": to_nldd_icon(definition.icon),
            "color": definition.color,
            "help_template": getattr(definition, "help_template", None),
            # De omgevingsvariabelen die deze service levert, met hun aliassen en hun
            # uitleg. De bestaande pagina toont die per kaart; ze stonden hier alleen
            # geTELD op een chip ("3 variabelen"), en dat is precies het soort
            # samenvatting waar niemand iets aan heeft: je komt op deze pagina om te
            # zien HOE de variabele heet die je in je applicatie moet uitlezen.
            "variables": entry["variables"],
        }
        for entry in services_info
        for definition in (entry["definition"],)
    ]

    # De uitleg van een dienst gaat NIET via de server. De bestaande pagina opent hem in
    # een dialoog (openServiceHelp() in static/js/wizard.js, dat de tekst bij
    # /forms/wizard/help/<template> ophaalt), en dat is wat de gebruiker kent. Een
    # ?help=-parameter die de uitleg inline op de pagina zet was hier zelf bedacht; hij is
    # weg, want twee wegen naar dezelfde uitleg lopen vroeg of laat uiteen.
    #
    # Hier stond ook een filterbalk (Alle / Zelf te kiezen / Altijd aan) en een chip per
    # kaart met de binding en het aantal variabelen. Allebei zelf bedacht: het origineel
    # heeft geen filter en geen chips. Weg, om dezelfde reden.
    return {
        "navigation": get_navigation(user, current_path="/services"),
        "services": services,
    }


def build_lotc_dashboard(*, user: dict[str, Any] | None, **_ongebruikt: Any) -> dict[str, Any]:
    """Wat het dashboard extra nodig heeft: alleen de navigatie.

    De route levert alle gegevens al - kerncijfers, gezondheid, metrics, projecten - en
    het sjabloon leest precies die sleutels. Ze hier omvormen zou een tweede vorm van
    dezelfde gegevens opleveren, en dan gaat de nieuwe pagina iets anders tonen dan de
    bestaande zodra er een veld bijkomt.
    """
    from opi.web.navigation_lotc import get_navigation

    return {"navigation": get_navigation(user, current_path="/dashboard")}


def build_lotc_admin(*, user: dict[str, Any] | None, current_path: str) -> dict[str, Any]:
    """Wat een beheerpagina extra nodig heeft: alleen de navigatie.

    Een functie voor alle vier de beheerpagina's (gebruikers, het gebruikersformulier,
    domeinbeheer en gebruik & kosten), want ze hebben alle vier hetzelfde nodig. Hun
    routes leveren de rest al in de vorm die de sjablonen lezen; die hier omvormen zou
    een tweede vorm van dezelfde gegevens opleveren, en dan gaat de nieuwe pagina iets
    anders tonen dan de bestaande zodra er een veld bijkomt.

    ``current_path`` bepaalt welk item in de zijkolom actief is en verschilt dus wel per
    pagina.
    """
    from opi.web.navigation_lotc import get_navigation

    return {"navigation": get_navigation(user, current_path=current_path)}


#: Waarop de projectenlijst gesorteerd kan worden. De sleutel staat in de URL
#: (``?sort=naam-af``), het label in het menu, en de derde waarde is de sorteersleutel.
#: Als lijst en niet als dict, omdat de VOLGORDE de volgorde in het menu is.
PROJECT_SORTERINGEN: list[tuple[str, str, Any]] = [
    ("naam", "Naam (A-Z)", lambda p: (p["display_name"] or p["name"]).lower()),
    ("naam-af", "Naam (Z-A)", lambda p: (p["display_name"] or p["name"]).lower()),
    ("deployments", "Meeste deployments", lambda p: -p["deployment_count"]),
    ("teamleden", "Meeste teamleden", lambda p: -len(p["users"])),
]


def build_lotc_projects(
    request: Request,
    *,
    user: dict[str, Any] | None,
    projects: list[dict[str, Any]],
) -> dict[str, Any]:
    """De ECHTE projectenlijst, in de vorm die de hertekende pagina leest.

    Hier stond een uitgeklede vorm: alleen naam, omschrijving en het aantal deployments.
    De bestaande pagina toont per project ook de OMGEVING, het TEAM (aantal plus
    initialen) en de SERVICES, en heeft daaronder vier totalen staan. Die vielen
    daardoor weg - niet omdat er een besluit over genomen was, maar omdat deze functie
    ze niet doorgaf. Alles wat de pagina toont komt nu uit deze ene vorm.

    Zoeken en sorteren gebeuren HIER en niet in de browser: dan werkt het ook zonder
    JavaScript, is een gefilterde lijst deelbaar als URL, en blijft de telling onder de
    tabel kloppen met wat er staat.
    """
    from opi.web.navigation_lotc import get_navigation

    return {
        "navigation": get_navigation(user, current_path="/projects"),
        **filter_lotc_projects(request, lotc_project_rows(projects)),
    }


def lotc_project_rows(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """De projectgegevens in de vorm die de tabel leest.

    Tellen op de LIJSTEN die de route levert, niet op een ``deployment_count``-sleutel.
    Die bestaat wel op het dashboard maar niet hier, en het gevolg was een overzicht
    waarin alles nul was terwijl er projecten met deployments stonden.
    """
    return [
        {
            "name": project["name"],
            "display_name": project.get("display_name") or project["name"],
            "description": project.get("description", ""),
            "users": project.get("users") or [],
            "services": project.get("services") or [],
            "clusters": project.get("clusters") or [],
            "deployment_count": len(project.get("deployments") or []),
        }
        for project in projects
    ]


def filter_lotc_projects(request: Request, projects: list[dict[str, Any]]) -> dict[str, Any]:
    """Pas ``?q=`` en ``?sort=`` toe, en lever alles wat de lijst en zijn balk nodig hebben.

    Apart van :func:`build_lotc_projects` omdat de htmx-route die alleen de TABEL
    teruggeeft precies dit stuk nodig heeft en niet de navigatie eromheen.
    """
    zoekterm = (request.query_params.get("q") or "").strip()
    if zoekterm:
        naald = zoekterm.lower()
        gevonden = [
            project
            for project in projects
            if naald in project["name"].lower()
            or naald in project["display_name"].lower()
            or naald in project["description"].lower()
        ]
    else:
        gevonden = list(projects)

    gekozen = request.query_params.get("sort") or PROJECT_SORTERINGEN[0][0]
    sleutel = next((s for k, _, s in PROJECT_SORTERINGEN if k == gekozen), PROJECT_SORTERINGEN[0][2])
    gevonden.sort(key=sleutel)
    if gekozen == "naam-af":
        gevonden.reverse()

    return {
        "projects": gevonden,
        # De totalen onderaan tellen over ALLE projecten van deze gebruiker, niet over
        # wat het zoekveld overlaat: "Je projecten" hoort niet te dalen omdat je iets
        # intypt. Alleen "Totaal" boven de tabel volgt de zoekterm, want die hoort bij
        # wat je ziet.
        "projects_all": projects,
        "project_query": zoekterm,
        "project_sort": gekozen,
        "project_sorteringen": [(sleutel, label) for sleutel, label, _ in PROJECT_SORTERINGEN],
    }


#: De tabbladen van de projectpagina.
#:
#: Het eerste tabblad heette "Project" en droeg negen blokken, van de kerncijfers tot de
#: gevarenzone. Componenten en Services staan nu apart; wat overblijft is de INGANG van
#: het project (hoe staat het ervoor, wie mag erbij, wat zijn de sleutels, hoe kom je
#: ervan af), en dat is wat "Overzicht" zegt.
#:
#: Helm charts en helmfiles staan bij Componenten en niet bij Overzicht: het zijn net zo
#: goed draaiende onderdelen die je op projectniveau declareert.
PROJECT_TABS = {
    "project": {"label": "Overzicht"},
    "componenten": {"label": "Componenten"},
    "services": {"label": "Services"},
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
    from opi.web.navigation_lotc import get_navigation

    requested = request.query_params.get("tab", "")
    return {
        "navigation": get_navigation(user, current_path="/projects"),
        "tabs": PROJECT_TABS,
        "active_tab": requested if requested in PROJECT_TABS else next(iter(PROJECT_TABS)),
        "project": project,
    }


#: Het koekje waarin de weergavekeuze (licht/donker) bewaard blijft.
#:
#: Zelfde patroon als de layoutschakelaar hierboven, en om dezelfde reden: de keuze hoort
#: bij de gebruiker en niet bij een pagina. Server-side onthouden in plaats van in
#: localStorage scheelt bovendien de flits die je krijgt als JavaScript het thema pas na
#: de eerste weergave zet.
SCHEME_COOKIE = "zad_scheme"

#: De drie standen. "" is systeem: dan zet de pagina geen data-scheme en volgt NLDD de
#: voorkeur van het besturingssysteem.
SCHEMES = ("", "light", "dark")


def chosen_scheme(request: Request) -> str:
    """De weergave die deze gebruiker koos: ``light``, ``dark``, of leeg voor systeem."""
    stored = request.cookies.get(SCHEME_COOKIE, "")
    return stored if stored in SCHEMES else ""
