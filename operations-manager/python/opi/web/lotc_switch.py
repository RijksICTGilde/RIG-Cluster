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
from urllib.parse import quote, urlencode

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
#: Waarop de deploymenttabel gesorteerd kan worden. Zelfde vorm als
#: :data:`PROJECT_SORTERINGEN` - sleutel in de URL (``?dsort=cluster``), label in het
#: menu, sorteersleutel als derde - en om dezelfde reden een LIJST: de volgorde hier is
#: de volgorde in het menu.
#:
#: Er wordt niet op STATUS gesorteerd. Die staat wel in de tabel (hij komt uit de
#: gebundelde bevraging in ``opi/services/argocd_overview.py``), maar sorteren erop zou
#: betekenen dat de sortering afhangt van een antwoord van een ander systeem: dezelfde
#: URL geeft dan morgen een andere volgorde, en een rij verspringt terwijl je kijkt.
#: Sorteren gebeurt daarom op wat in het projectbestand staat.
DEPLOYMENT_SORTERINGEN: list[tuple[str, str, Any]] = [
    ("naam", "Naam (A-Z)", lambda d: d["name"].lower()),
    ("naam-af", "Naam (Z-A)", lambda d: d["name"].lower()),
    ("cluster", "Cluster", lambda d: ((d.get("cluster") or "").lower(), d["name"].lower())),
    ("componenten", "Meeste componenten", lambda d: (-len(d.get("components") or []), d["name"].lower())),
]


def filter_lotc_deployments(
    request: Request, deployments: list[dict[str, Any]], deployment_open: str = ""
) -> dict[str, Any]:
    """Pas ``?q=`` en ``?dsort=`` toe op de deployments van een project.

    Zelfde opzet als :func:`filter_lotc_projects`, en om dezelfde reden SERVER-SIDE: dan
    werkt zoeken en sorteren ook zonder JavaScript, is een gefilterde lijst deelbaar als
    URL, en kan de telling boven de tabel niet uit de pas lopen met de rijen eronder.

    ``dsort`` en niet ``sort``: de projectenlijst gebruikt ``sort`` al, en beide namen
    kunnen in een en dezelfde URL staan zodra iemand een link deelt. Het ZOEKveld heet
    wel gewoon ``q`` - die twee lijsten staan nooit op dezelfde pagina.

    WELKE deployment open staat wordt hier NIET meer bepaald. Dat doet de route, uit het
    PAD (``/projects/<project>/deployments/<naam>``), met :func:`kies_deployment`; het
    komt hier binnen als ``deployment_open``. De tabel op Overzicht heeft die keuze zelf
    niet nodig - daar is elke rij een link naar het tabblad Deployments - maar de kiezer
    en het paneel lezen hem uit dezelfde context, dus hij gaat mee de pagina in.
    """
    zoekterm = (request.query_params.get("q") or "").strip()
    if zoekterm:
        naald = zoekterm.lower()
        gevonden = [
            deployment
            for deployment in deployments
            if naald in deployment["name"].lower() or naald in (deployment.get("cluster") or "").lower()
        ]
    else:
        gevonden = list(deployments)

    # Een onbekende ``dsort`` valt terug op de EERSTE sortering, en die naam gaat ook
    # terug de pagina in. Hij bleef hier eerst staan zoals hij binnenkwam, waardoor het
    # verborgen veld en hx-push-url een sortering beloofden die niet gebruikt werd: de
    # URL zei ``dsort=onzin`` terwijl de lijst op naam stond.
    gevraagd_sort = request.query_params.get("dsort") or ""
    bekend = {k for k, _, _ in DEPLOYMENT_SORTERINGEN}
    gekozen = gevraagd_sort if gevraagd_sort in bekend else DEPLOYMENT_SORTERINGEN[0][0]
    sleutel = next(s for k, _, s in DEPLOYMENT_SORTERINGEN if k == gekozen)
    gevonden.sort(key=sleutel)
    if gekozen == "naam-af":
        gevonden.reverse()

    geopend = next((deployment for deployment in deployments if deployment["name"] == deployment_open), None)

    return {
        "deployments_zichtbaar": gevonden,
        # De telling boven de tabel volgt de zoekterm ("3 van 12"); daarvoor is het
        # totaal over ALLE deployments nodig, niet over wat het zoekveld overlaat.
        "deployments_alle": deployments,
        "deployment_query": zoekterm,
        "deployment_sort": gekozen,
        "deployment_sorteringen": [(sleutel, label) for sleutel, label, _ in DEPLOYMENT_SORTERINGEN],
        "deployment_open": deployment_open,
        # De deployment zelf, want de tabbladen renderen er nog maar EEN en hoeven er dus
        # niet meer met een lus doorheen om hem te vinden. Leeg als het project er geen
        # heeft; dan toont het tabblad de melding "Nog geen deployments".
        "deployment_geopend": geopend,
    }


def deployment_status_tags(
    *,
    replacing_badges: list[str],
    accompanying_badges: list[str],
    argocd: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """De statuslabels van EEN rij in de deploymenttabel: ``[{'label': .., 'type': ..}]``.

    Twee bronnen, en ze horen bij elkaar in een kolom:

      - wat de DIENSTEN melden (slaapstand, uitgeschakeld). Die berekent de pagina toch
        al, per deployment, zonder een enkel extra verzoek.
      - de gezondheid en de sync van ARGOCD, uit de gebundelde bevraging
        (``opi/services/argocd_overview.py``): een verzoek voor het hele project, dus
        twintig rijen kosten er nog steeds een.

    De REGEL waarmee ze samenkomen is die van de statuskaart in het paneel (RC-31/RC-35),
    en staat hier in Python in plaats van in twee sjablonen: een dienstbadge neemt de
    plaats in van precies EEN verdict, de groene "Healthy" die nul replicas oplevert. Elk
    ander verdict - Degraded, Progressing, Unknown - heeft ArgoCD echt waargenomen en
    houdt zijn badge, anders verbergt het uitzetten van een component een echte storing.

    ``argocd`` is ``None`` als ArgoCD niet verbonden is. Dan staan hier alleen de
    dienstbadges: de pagina zegt er in gewone taal bij dat de status ontbreekt, en daar
    een verzonnen "Unknown" per rij overheen zetten maakt dat onleesbaar.
    """
    tags: list[dict[str, str]] = [{"label": badge, "type": "info"} for badge in accompanying_badges]
    tags += [{"label": badge, "type": "info"} for badge in replacing_badges]

    if argocd is None:
        return tags

    if not argocd.get("available"):
        # ArgoCD kent deze applicatie niet. Dat is iets anders dan "ongezond": er is nog
        # niets uitgerold, of de uitrol is er nog niet doorheen.
        return [*tags, {"label": "Niet in ArgoCD", "type": "default"}]

    health = argocd.get("health", "Unknown")
    sync = argocd.get("sync", "Unknown")

    if not (replacing_badges and health == "Healthy"):
        tags.append(
            {
                "label": health,
                "type": "success" if health == "Healthy" else "warning" if health == "Progressing" else "error",
            }
        )

    # Unknown betekent dat ArgoCD gewenste en werkelijke staat niet kan vergelijken (vaak
    # een render- of CMP-fout) - rood dus, niet neutraal grijs, anders leest het als "nog
    # mee bezig".
    tags.append(
        {
            "label": sync,
            "type": "success"
            if sync == "Synced"
            else "warning"
            if sync == "OutOfSync"
            else "error"
            if sync == "Unknown"
            else "default",
        }
    )
    return tags


def build_deployment_status_column(
    deployments: list[dict[str, Any]],
    deployment_states: dict[str, Any],
    argocd_statuses: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    """De statuskolom van de hele tabel: deploymentnaam -> labels.

    Een dict en geen berekening in het sjabloon, zodat de regel uit
    :func:`deployment_status_tags` op EEN plek staat en te toetsen is zonder een pagina
    te renderen.
    """
    return {
        naam: deployment_status_tags(
            replacing_badges=state.replacing_badges if state else [],
            accompanying_badges=state.accompanying_badges if state else [],
            argocd=argocd_statuses.get(naam) if argocd_statuses else None,
        )
        for deployment in deployments
        for naam in (deployment["name"],)
        for state in (deployment_states.get(naam),)
    }


#:
#: Elk tabblad heeft een EIGEN PAD, geen ``?tab=``-parameter. Een querystring leest als
#: een filter op een pagina ("laat hiervan alleen dit zien"), en dat is een tabblad niet:
#: het is een andere pagina over hetzelfde project. Het pad zegt dat, een parameter niet.
#:
#: ``path`` staat naast het label omdat het adres NIET af te leiden is uit de sleutel:
#: Overzicht heet ``project`` en woont op ``details``.
#:
#: De PROJECTNAAM staat voorop en het tabblad erachter: ``/projects/<naam>/deployments``.
#: Het project is waar je bent, het tabblad is wat je erbinnen bekijkt, en zo leest het
#: pad ook. De oude vorm (``/projects/deployments/<naam>``) verwijst door, zodat gedeelde
#: links blijven werken.
PROJECT_TABS = {
    "project": {"label": "Overzicht", "path": "details"},
    "team": {"label": "Team", "path": "team"},
    "componenten": {"label": "Componenten", "path": "componenten"},
    "services": {"label": "Services", "path": "services"},
    "deployments": {"label": "Deployments", "path": "deployments"},
    "metrics": {"label": "Metrics", "path": "metrics"},
    "backups": {"label": "Backups", "path": "backups"},
    "taken": {"label": "Taken", "path": "taken"},
}

#: Het tabblad waar een onbekend (of ontbrekend) tabblad op uitkomt.
STANDAARD_TAB = next(iter(PROJECT_TABS))

#: De tabbladen die EEN deployment tegelijk tonen, en die hem daarom in hun PAD dragen:
#: ``/projects/<project>/deployments/<deployment>``.
#:
#: Ze stonden er allebei alle deployments te renderen en verborgen er alles behalve een
#: met CSS. Dat kost werk voor blokken die niemand ziet, en de keuze ging verloren zodra
#: je van tabblad wisselde. Nu rendert de server er een, en zegt de URL welke: dat is
#: deelbaar, de terugknop werkt, en de keuze reist mee naar het andere tabblad omdat de
#: tabbalk hem in zijn adressen meeneemt.
TABS_MET_DEPLOYMENT = ("deployments", "metrics", "backups")


def project_tab_url(project_name: str, tab: str, query: str = "", deployment: str = "") -> str:
    """Het adres van een tabblad van een project.

    Op een plek berekend, zodat de tabbalk, de kruimels, de kerncijfers en de router het
    over hetzelfde adres hebben. Een onbekend tabblad valt terug op Overzicht in plaats
    van een pad te verzinnen dat geen route heeft.

    ``deployment`` komt er als extra segment achter, maar alleen op de tabbladen die er
    een tegelijk tonen (:data:`TABS_MET_DEPLOYMENT`). Op Overzicht zou dat pad geen route
    hebben, en dan zou de tabbalk naar een 404 wijzen zodra er een deployment gekozen is.
    """
    pad = PROJECT_TABS.get(tab, PROJECT_TABS[STANDAARD_TAB])["path"]
    adres = f"/projects/{project_name}/{pad}"
    if deployment and tab in TABS_MET_DEPLOYMENT:
        adres += f"/{quote(deployment, safe='')}"
    return adres + (f"?{query}" if query else "")


def kies_deployment(namen: list[str], *voorkeuren: str) -> str:
    """Welke deployment staat er open: de eerste voorkeur die ECHT bestaat.

    Een naam die niets oplevert - een verwijderde deployment in een gedeelde link - valt
    terug op de eerste op naam, want zo staan ze in de kiezer. Een project zonder
    deployments levert een lege naam op; daar valt niets te openen.

    Dit is de ENE regel voor "welke staat open". Hij stond eerder half in de server
    (``?deployment=``) en half in de browser (een onthouden keuze in sessionStorage), en
    die twee konden elkaar overschrijven.
    """
    for voorkeur in voorkeuren:
        if voorkeur and voorkeur in namen:
            return voorkeur
    return sorted(namen)[0] if namen else ""


def deployment_pagina_adres(request: Request, project_name: str, deployment_open: str) -> str:
    """Het adres waar DEZE pagina hoort te staan, met de deployment in het pad.

    De querystring gaat mee behalve ``deployment=``: die oude vorm (een gedeelde link uit
    de tijd dat de keuze een parameter was) wordt hiermee een doorverwijzing naar het pad,
    zodat er een adres per deployment is en niet twee.
    """
    tab = tab_from_path(request.url.path)
    resterend = [(sleutel, waarde) for sleutel, waarde in request.query_params.multi_items() if sleutel != "deployment"]
    return project_tab_url(project_name, tab, urlencode(resterend), deployment=deployment_open)


def tab_from_path(path: str) -> str:
    """Welk tabblad hoort bij dit pad? ``/projects/<naam>/deployments`` -> ``deployments``.

    Leest het DERDE segment (het tabblad staat achter de projectnaam) en niet de naam van
    de route, zodat een pad dat er niet bij hoort op Overzicht uitkomt in plaats van een
    fout te geven.
    """
    delen = path.strip("/").split("/")
    segment = delen[2] if len(delen) > 2 else ""
    return next((tab for tab, gegevens in PROJECT_TABS.items() if gegevens["path"] == segment), STANDAARD_TAB)


def build_lotc_project_details(
    request: Request,
    *,
    user: dict[str, Any] | None,
    project: dict[str, Any],
    deployment_open: str = "",
) -> dict[str, Any]:
    """De ECHTE projectgegevens, in de vorm die de pagina met tabs leest.

    Er wordt niets omgerekend: ``project`` heeft al de vorm die de route opbouwt, en het
    sjabloon leest precies die sleutels. Wat hier gebeurt is de navigatie en het actieve
    tabblad bepalen.

    Het actieve tabblad komt uit het PAD (``/projects/<naam>/deployments``) en niet meer
    uit ``?tab=``. Die oude vorm is weg, ook als doorverwijzing: hij heeft nooit buiten
    deze applicatie geleefd, en een tweede adres dat niemand gebruikt is onderhoud zonder
    lezer. Een achtergebleven ``?tab=`` wordt genegeerd.

    Het resourcegebruik zit hier NIET in. De bestaande pagina laadt dat apart met htmx,
    zodat een trage Prometheus de pagina niet ophoudt, en dat blijft zo - het fragment
    kent zijn eigen LOTC-weergave.
    """
    from opi.web.navigation_lotc import get_navigation

    active_tab = tab_from_path(request.url.path)
    return {
        "navigation": get_navigation(user, current_path="/projects"),
        # De gekozen deployment gaat MEE in de tabbladadressen. Zo blijft hij staan als je
        # van Deployments naar Metingen wisselt: dat is een gewone link naar een adres dat
        # de naam al draagt, en niet iets dat de browser moet onthouden.
        "tabs": {
            tab: {**gegevens, "url": project_tab_url(project["name"], tab, deployment=deployment_open)}
            for tab, gegevens in PROJECT_TABS.items()
        },
        "active_tab": active_tab,
        "project": project,
        **filter_lotc_deployments(request, project.get("deployments") or [], deployment_open),
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
