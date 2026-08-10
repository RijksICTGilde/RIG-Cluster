"""Vergelijk het GEDRAG van een pagina in de oude en de nieuwe vormgeving.

De omzetting mag de pagina er anders uit laten zien. Wat hij niet mag, is dingen laten
doen die de oude pagina niet deed, of - veel vaker - dingen NIET meer doen. Dat verschil
is niet met het blote oog te zien: een verdwenen keuzelijst of een knop die iets anders
aanroept valt pas op als je hem nodig hebt.

Daarom haalt dit script dezelfde route twee keer op, met ?layout=roos en ?layout=nldd, en
legt het gedragsoppervlak naast elkaar:

    - elke bestemming: href, form action
    - elk htmx-adres: hx-get, hx-post, hx-delete, hx-put, hx-patch
    - elke aangeroepen JavaScript-functie: uit onclick, @click, onchange, oninput
    - elk besturingselement met een naam: input, select, textarea
    - elk id waar JavaScript of htmx aan kan hangen

Vormgeving telt daarbij NIET mee: tagnamen, klassen en teksten verschillen per definitie.
Wat overblijft is wat de pagina DOET.

Gebruik:

    uv run python scripts/lotc_compare_behaviour.py --base https://zad.sandbox.rijksapp.dev

De sessiecookie komt uit --secret (de SECRET_KEY van de draaiende applicatie) plus
--email (een adres dat op de allowlist staat). Zonder die twee krijg je de loginpagina
terug, en dan lijkt elke pagina op elke andere - een meting die overal hetzelfde zegt
meet niets.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from html.parser import HTMLParser

import httpx
from itsdangerous import TimestampSigner

# De routes die omgezet zijn. Per regel: het pad, en of hij een project nodig heeft.
ROUTES = [
    "/dashboard",
    "/projects",
    "/services",
    "/admin/users",
    "/admin/approvals",
    "/admin/usage",
    "/metrics-explorer",
    "/about",
    "/forms/wizard/start",
]

# De fragmenten die pas na een klik of via htmx in beeld komen. Ze staan hier apart omdat
# ze geen pagina zijn: ze worden IN een pagina of IN de gedeelde dialoog gezet. Juist
# daar glipt een omzetting langs de aandacht - het venster opent, en pas als je erin kijkt
# staat er de oude vormgeving. Wat er in staat moet net zo goed kloppen als een pagina:
# elk hx-post, elke veldnaam en elk id draagt hier het OPSLAAN.
FRAGMENTEN = [
    "/projects/details/{project}/backups",
    "/projects/{project}/tasks",
    "/projects/{project}/modal-wizard/modal-edit-identity",
    "/projects/{project}/modal-wizard/modal-edit-team",
    "/projects/{project}/modal-wizard/modal-edit-services",
    "/projects/{project}/actions/refresh-project/confirm",
]

# De projectpagina is een geval apart. De oude pagina zet alle tabbladen in EEN document
# en wisselt ze in de browser; de nieuwe geeft elk tabblad een eigen URL. Een tab los
# vergelijken met de hele oude pagina meldt dus alles van de andere tabs als verdwenen.
# Daarom wordt de oude pagina vergeleken met de VERENIGING van de nieuwe tabbladen.
TABPAGINA = "/projects/details/{project}"
TABBLADEN = ("project", "deployments", "metrics", "taken")

HX_ATTRS = ("hx-get", "hx-post", "hx-delete", "hx-put", "hx-patch")

# Waarden die per verzoek verschillen en dus niets zeggen over gedrag: het wizard-token,
# CSRF, cache-brekers. Zonder dit meldt elke vergelijking van een dialoog een verschil dat
# er niet is - en een meetlat die altijd piept, houdt niemand in de gaten.
VLUCHTIG = re.compile(r"((?:_wizard_token|csrf|csrf_token|nonce|_ts|v)=)[^&\s\"']+", re.IGNORECASE)


def stabiel(waarde: str) -> str:
    """Vervang wat per verzoek verschilt door een vaste tekst."""
    return VLUCHTIG.sub(r"\1<wisselend>", waarde)


JS_ATTRS = ("onclick", "@click", "onchange", "oninput", "onsubmit")

# Aanroepen die alleen over vormgeving gaan en dus mogen verschillen.
JS_NEGEER = {"switchTab"}


# Bestemmingen die per definitie verschillen: elk design system brengt zijn eigen
# stylesheets en scripts mee. Dat is geen gedrag, dat is de vormgeving zelf.
# Verschillen die we aanvaarden, elk met de reden erbij. Een regel hier is een BESLUIT.
AANVAARD: dict[str, str] = {
    # De gebruiker heeft hier zelf om gevraagd: "wat sowieso niet getoond hoeft te worden
    # is repositories".
    "repositories": "op verzoek van de gebruiker niet overgenomen",
    # De oude pagina wisselt tabbladen in de browser; de nieuwe geeft elk tabblad een
    # eigen URL, zodat een tab deelbaar is en de terugknop werkt.
    "switchTab": "tabs zijn echte links geworden",
    # De metrics-explorer bindt zijn knop met addEventListener in plaats van met een
    # onclick-attribuut, omdat <c-button> geen onclick doorlaat. Zelfde gedrag.
    "showMetric": "gebonden met addEventListener in plaats van een onclick-attribuut",
    # De knoppenbalk onderin de gedeelde dialoog is DODE markup: "Opslaan" roept
    # submitEditModal() aan, en die functie bestaat nergens in de codebase. Zichtbaar was
    # hij ook nooit - de modal-wizard vervangt de hele #edit-section-inner zodra hij laadt,
    # inclusief die balk. Zulke markup neem je bij een verhuizing niet mee.
    "submitEditModal": "riep een functie aan die niet bestaat; nooit zichtbaar geweest",
    "edit-section-submit": "hoort bij die dode knoppenbalk",
    "edit-section-actions": "hoort bij die dode knoppenbalk",
    # Het dashboard toonde ook de LIJST met projecten, met een link per project. Die staat
    # op /projects, en twee plekken met dezelfde lijst lopen vroeg of laat uiteen. Op
    # verzoek van de gebruiker vat het dashboard nu samen - kerncijfers en resourcegebruik -
    # met een knop "Alle projecten" als ingang. De bestemmingen zelf zijn dus niet
    # verdwenen, alleen niet meer op deze pagina.
    "/projects/details/": "de projectenlijst staat op /projects, niet meer op het dashboard",
    # De drie tabbladwikkels van de oude pagina. Die bestaan alleen omdat alle tabs daar in
    # een document staan en switchTab() ze toont en verbergt; met een eigen URL per tab is
    # er niets te tonen of te verbergen.
    "tab-project": "de oude tabwikkel; elk tabblad heeft nu een eigen URL",
    "tab-deployments": "de oude tabwikkel; elk tabblad heeft nu een eigen URL",
    "tab-taken": "de oude tabwikkel; elk tabblad heeft nu een eigen URL",
    # Hetzelfde verhaal: de oude pagina laadt de takenlijst pas als switchTab('taken') hem
    # aanwijst. Het nieuwe tabblad haalt hem bij het laden op - zelfde lijst, zelfde URL,
    # zonder dat er een tweede plek is die weet wanneer het moet.
    "tasks-content": "de takenlijst laadt nu bij het openen van zijn eigen tabblad",
    # De knop van de jobdialoog doet zijn verzoek zelf, met hx-include, in plaats van een
    # <form> in te dienen. Niet uit voorkeur: <c-form> rendert een <nldd-form> (een
    # web-component, geen formulier), en met een echte <form> eromheen bereikte de klik van
    # de <nldd-button> het submit-event niet - ook form.requestSubmit() leverde geen enkel
    # verzoek op. De consoledialoog ernaast doet het in BEIDE vormgevingen al zo.
    "hx-include=#job-form-": "de knop doet het verzoek zelf; een verzendknop in een form deed daar niets",
    # Ongebruikte markup: log_viewer.js noemt log-pause-icon nergens. De pauzeknop zelf
    # (log-pause-btn) staat er wel en werkt.
    "log-pause-icon": "dode markup; het script gebruikt dit id niet",
    # applyRules() is hoe het geheim-veld van ROOS zichzelf toont en verbergt - code van
    # dat design system, niet van ons. Het LOTC-veld doet hetzelfde met zijn eigen
    # mechanisme (en heeft er een kopieerknop bij). Het gedrag is er dus wel; alleen de
    # naam van de functie die het uitvoert hoort bij de vormgeving.
    "applyRules": "de interne implementatie van het geheim-veld van roos; LOTC heeft zijn eigen",
    # De uitklapbare foutenlijst van een ArgoCD-kaart was een knop plus een paneel dat
    # toggleArgoErrors() zichtbaar maakte. Nu een <details>: hetzelfde uitklappen, zonder
    # dat de pagina daar JavaScript voor hoeft mee te brengen.
    "toggleArgoErrors": "uitklappen gaat nu met <details>, zonder JavaScript",
    # De id-voorvoegsels van die foutenlijsten. Op de oude pagina staan projecttab en
    # deploymentstab in EEN document, dus moesten de id's daar uit elkaar gehouden worden
    # met dep-. Met een eigen URL per tabblad botst er niets meer.
    "argocd-errors-dep-": "voorvoegsel was er om twee tabbladen in een document te scheiden",
    "?prefix=dep-": "voorvoegsel was er om twee tabbladen in een document te scheiden",
}


#: Achtervoegsels van id's die een design system ZELF bijmaakt om een veld aan zijn label
#: en zijn hulptekst te knopen (aria-describedby, label for). Ze zijn interne bedrading en
#: geen gedrag: niemand mikt er een link, een htmx-doel of een JavaScript-aanroep op.
#:
#: Ze horen hier en niet in AANVAARD, want dat zijn BESLUITEN over een verschil dat we
#: accepteren; dit is een verschil dat er niet is. Roos maakt ``<veld>-label``, NLDD maakt
#: ``<veld>-help`` en ``<veld>-error``, en met beide in de meting meldt elke vergelijking
#: van een formulier een handvol verschillen die niemand kan wegnemen - waarna je de
#: meetlat gaat negeren.
AFGELEIDE_ID_ACHTERVOEGSELS = ("-label", "-help", "-error", "-helper", "-description")


def is_afgeleid_id(waarde: str) -> bool:
    return waarde.endswith(AFGELEIDE_ID_ACHTERVOEGSELS)


#: NLDD-web-componenten die een echt formulierveld ZIJN, naast de ``*-field``-familie.
#: Op "elke nldd-tag met een name" gaat het mis: ``<nldd-icon name="envelope">`` draagt
#: daar de ICOONNAAM, en dan telt elk icoon als een veld.
NLDD_BESTURING = {"nldd-select", "nldd-textarea", "nldd-checkbox", "nldd-radio", "nldd-switch", "nldd-combo-box"}


def is_nldd_besturing(tag: str) -> bool:
    return tag in NLDD_BESTURING or (tag.startswith("nldd-") and tag.endswith("-field"))


def is_ruis(bestemming: str) -> bool:
    return bestemming.startswith(("/static/roos/", "/static/lotc/", "/static/css/", "/static/js/")) or (
        "unpkg.com" in bestemming
    )


class Oppervlak(HTMLParser):
    """Verzamelt wat een pagina DOET, los van hoe hij eruitziet."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.bestemmingen: set[str] = set()
        self.htmx: set[str] = set()
        self.functies: set[str] = set()
        self.velden: set[str] = set()
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        d = {k: (v or "") for k, v in attrs}

        # Elk thema noemt zijn link anders: roos schrijft href, NLDD schrijft op sommige
        # componenten website-href. Op de naam "href" alleen zou de thuislink hier als
        # verdwenen gemeld worden terwijl hij er gewoon staat - een vals alarm, en dat is
        # erger dan geen meting.
        for sleutel, waarde in d.items():
            if sleutel not in ("href", "action") and not sleutel.endswith("-href"):
                continue
            # Een anker of een javascript:-link is navigatie binnen de pagina, geen bestemming.
            if waarde and not waarde.startswith(("#", "javascript:")) and not is_ruis(waarde):
                self.bestemmingen.add(stabiel(waarde))

        for sleutel in HX_ATTRS:
            if d.get(sleutel):
                self.htmx.add(f"{sleutel}={stabiel(d[sleutel])}")

        for sleutel in JS_ATTRS:
            for naam in re.findall(r"([A-Za-z_$][\w$]*)\s*\(", d.get(sleutel, "")):
                if naam not in JS_NEGEER:
                    self.functies.add(naam)

        # NLDD levert zijn invoervelden als web-componenten (``<nldd-text-field>``,
        # ``<nldd-select>``, ...) en niet als een <input>. Die stonden hier niet bij, dus
        # een LOTC-formulier meette NUL velden en een verdwenen veld kwam er als "gelijk"
        # uit - precies het valse groen waar deze meetlat tegen bedoeld is. Gemeten op de
        # jobdialoog: die heeft twee velden (image, command) en de vergelijking meldde ze
        # als weg terwijl ze er gewoon stonden.
        is_besturing = (
            tag in ("input", "select", "textarea")
            or is_nldd_besturing(tag)
            or d.get("data-lotc-component") in ("text-input", "select-field", "textarea")
        )
        if is_besturing and d.get("name"):
            self.velden.add(d["name"])

        # Het id van zo'n web-component staat op ``input-id``: dat is het id dat het echte
        # <input> straks draagt, en dus waar een label of een hx-include naar wijst.
        for sleutel in ("id", "input-id"):
            if d.get(sleutel) and not is_afgeleid_id(d[sleutel]):
                self.ids.add(d[sleutel])


def haal_op(client: httpx.Client, basis: str, pad: str, layout: str) -> str:
    scheider = "&" if "?" in pad else "?"
    r = client.get(f"{basis}{pad}{scheider}layout={layout}")
    r.raise_for_status()
    return r.text


def meet_met_fragmenten(client: httpx.Client, basis: str, pad: str, layout: str) -> Oppervlak:
    """Meet de pagina PLUS wat hij zelf met htmx inlaadt.

    Zonder dit meet je een pagina die zijn werk uitstelt te laag. Het dashboard haalt zijn
    meters bijvoorbeeld apart op - bewust, want inline was het traag - en dan meldt een
    vergelijking die alleen de pagina leest dat die meters verdwenen zijn terwijl ze er
    gewoon zijn. Dat is een vals alarm, en daar leer je een meetlat van negeren.

    Een niveau diep, en alleen hx-get: dat is ophalen en dus veilig te herhalen. Een
    fragment dat zelf weer iets inlaadt telt niet mee; tot nu toe komt dat niet voor, en
    ongelimiteerd doorlopen zou van een meting een kruiptocht maken.
    """
    oppervlak = meet(haal_op(client, basis, pad, layout))

    for item in sorted(oppervlak.htmx):
        if not item.startswith("hx-get="):
            continue
        url = item[len("hx-get=") :]
        if not url.startswith("/"):
            continue
        try:
            deel = meet(haal_op(client, basis, url, layout))
        except httpx.HTTPError:
            continue
        oppervlak.bestemmingen |= deel.bestemmingen
        oppervlak.functies |= deel.functies
        oppervlak.velden |= deel.velden
        oppervlak.ids |= deel.ids
        # De htmx-adressen van een fragment NIET overnemen: die zouden een volgende ronde
        # uitlokken, en het gaat hier om wat de pagina kan, niet hoe diep hij nest.

    return oppervlak


def meet(html: str) -> Oppervlak:
    o = Oppervlak()
    o.feed(html)
    return o


def vergelijk(oud: Oppervlak, nieuw: Oppervlak) -> list[str]:
    regels: list[str] = []
    for label, a, b in (
        ("bestemming", oud.bestemmingen, nieuw.bestemmingen),
        ("htmx", oud.htmx, nieuw.htmx),
        ("js-functie", oud.functies, nieuw.functies),
        ("veld", oud.velden, nieuw.velden),
        ("id", oud.ids, nieuw.ids),
    ):
        regels.extend(
            f"  WEG      {label:11s} {weg}" for weg in sorted(a - b) if not any(sleutel in weg for sleutel in AANVAARD)
        )
        # AANVAARD geldt beide kanten op. Hij filterde alleen wat WEG was, en dat is de
        # helft van het verhaal: een omzetting kan ook iets TOEVOEGEN dat we bewust
        # aanvaarden. De jobdialoog is zo'n geval - zijn knop doet het verzoek zelf, met een
        # hx-include, omdat een verzendknop in een <form> daar aantoonbaar niets deed. Zonder
        # deze regel is de enige uitweg de meetlat uitzetten, en dat is precies wat je niet
        # wilt: een verschil dat je aanvaardt hoort opgeschreven te staan, niet weggeklikt.
        regels.extend(
            f"  NIEUW    {label:11s} {erbij}"
            for erbij in sorted(b - a)
            if not any(sleutel in erbij for sleutel in AANVAARD)
        )
    return regels


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", required=True)
    p.add_argument("--secret", required=True)
    p.add_argument("--email", required=True)
    p.add_argument("--project", default=None, help="projectnaam voor de detailroutes")
    p.add_argument("--only", default=None, help="alleen routes die deze tekst bevatten")
    args = p.parse_args()

    payload = base64.b64encode(
        json.dumps({"user": {"sub": "vergelijker", "email": args.email, "name": "Vergelijker"}}).encode()
    ).decode()
    cookie = TimestampSigner(args.secret).sign(payload).decode()
    # verify=False: de sandbox draait met een eigen certificaat. Dit script praat alleen
    # met een omgeving die je zelf opgeeft, en leest; het verstuurt niets.
    client = httpx.Client(
        verify=False,  # noqa: S501
        follow_redirects=True,
        cookies={"session": cookie},
        timeout=120,
    )

    project = args.project
    if project is None:
        lijst = client.get(f"{args.base}/projects").text
        namen = re.findall(r"/projects/details/([a-z0-9-]+)", lijst)
        project = namen[0] if namen else ""

    def verenig(oppervlakken: list[Oppervlak]) -> Oppervlak:
        heel = Oppervlak()
        for o in oppervlakken:
            heel.bestemmingen |= o.bestemmingen
            heel.htmx |= o.htmx
            heel.functies |= o.functies
            heel.velden |= o.velden
            heel.ids |= o.ids
        return heel

    totaal = 0
    for route in ROUTES:
        pad = route.format(project=project)
        if args.only and args.only not in pad:
            continue
        try:
            oud = meet_met_fragmenten(client, args.base, pad, "roos")
            nieuw = meet_met_fragmenten(client, args.base, pad, "nldd")
        except httpx.HTTPError as e:
            print(f"{pad}\n  FOUT bij ophalen: {e}\n")
            continue

        regels = vergelijk(oud, nieuw)
        weg = [r for r in regels if r.startswith("  WEG")]
        totaal += len(weg)
        print(f"{pad}  ({len(weg)} weg, {len(regels) - len(weg)} nieuw)")
        for r in regels:
            print(r)
        print()

    for route in FRAGMENTEN:
        pad = route.format(project=project)
        if args.only and args.only not in pad:
            continue
        try:
            oud = meet_met_fragmenten(client, args.base, pad, "roos")
            nieuw = meet_met_fragmenten(client, args.base, pad, "nldd")
        except httpx.HTTPError as e:
            print(f"{pad}\n  FOUT bij ophalen: {e}\n")
            continue

        regels = vergelijk(oud, nieuw)
        weg = [r for r in regels if r.startswith("  WEG")]
        totaal += len(weg)
        print(f"{pad}  (fragment; {len(weg)} weg, {len(regels) - len(weg)} nieuw)")
        for r in regels:
            print(r)
        print()

    pad = TABPAGINA.format(project=project)
    if not args.only or args.only in pad:
        oud = meet_met_fragmenten(client, args.base, pad, "roos")
        nieuw = verenig([meet_met_fragmenten(client, args.base, f"{pad}?tab={t}", "nldd") for t in TABBLADEN])
        regels = vergelijk(oud, nieuw)
        weg = [r for r in regels if r.startswith("  WEG")]
        totaal += len(weg)
        print(f"{pad}  (alle tabbladen samen; {len(weg)} weg, {len(regels) - len(weg)} nieuw)")
        for r in regels:
            print(r)
        print()

    print(f"TOTAAL VERDWENEN GEDRAG: {totaal}")

    # Tweede meting, en die is er omdat de eerste hem NIET kan doen. De vergelijking
    # hierboven vindt gedrag dat VERDWENEN is. Een pagina die nog helemaal niet omgezet
    # is, rendert in beide weergaven hetzelfde en komt daar dus als schoon uit - terwijl
    # hij juist nog moet gebeuren. Dat is precies hoe je de indruk krijgt dat je klaar
    # bent terwijl de helft van de dialogen nog de oude vormgeving toont.
    #
    # Dit telt daarom simpelweg hoeveel elementen van elk systeem er in de nieuwe
    # weergave staan. Staat er nog roos in, dan is dat blok nog niet om.
    print()
    print("NOG NIET OMGEZET (nieuwe weergave bevat nog markup van het oude systeem):")
    achterstand = 0
    alles = [*ROUTES, *FRAGMENTEN, TABPAGINA]
    for route in alles:
        pad = route.format(project=project)
        if args.only and args.only not in pad:
            continue
        try:
            html = haal_op(client, args.base, pad, "nldd")
        except httpx.HTTPError:
            continue
        nldd = html.count("<nldd-")
        roos = html.count("rvo-")
        if roos > nldd:
            achterstand += 1
            print(f"  {pad:58s} nldd={nldd:4d}  oud={roos:4d}")
    if not achterstand:
        print("  (niets)")

    return 1 if totaal or achterstand else 0


if __name__ == "__main__":
    sys.exit(main())
