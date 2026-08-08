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

# De projectpagina is een geval apart. De oude pagina zet alle tabbladen in EEN document
# en wisselt ze in de browser; de nieuwe geeft elk tabblad een eigen URL. Een tab los
# vergelijken met de hele oude pagina meldt dus alles van de andere tabs als verdwenen.
# Daarom wordt de oude pagina vergeleken met de VERENIGING van de nieuwe tabbladen.
TABPAGINA = "/projects/details/{project}"
TABBLADEN = ("project", "deployments", "metrics", "taken")

HX_ATTRS = ("hx-get", "hx-post", "hx-delete", "hx-put", "hx-patch")
JS_ATTRS = ("onclick", "@click", "onchange", "oninput", "onsubmit")

# Aanroepen die alleen over vormgeving gaan en dus mogen verschillen.
JS_NEGEER = {"switchTab"}


# Bestemmingen die per definitie verschillen: elk design system brengt zijn eigen
# stylesheets en scripts mee. Dat is geen gedrag, dat is de vormgeving zelf.
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
                self.bestemmingen.add(waarde)

        for sleutel in HX_ATTRS:
            if d.get(sleutel):
                self.htmx.add(f"{sleutel}={d[sleutel]}")

        for sleutel in JS_ATTRS:
            for naam in re.findall(r"([A-Za-z_$][\w$]*)\s*\(", d.get(sleutel, "")):
                if naam not in JS_NEGEER:
                    self.functies.add(naam)

        is_besturing = tag in ("input", "select", "textarea") or d.get("data-lotc-component") in (
            "text-input",
            "select-field",
            "textarea",
        )
        if is_besturing and d.get("name"):
            self.velden.add(d["name"])

        if d.get("id"):
            self.ids.add(d["id"])


def haal_op(client: httpx.Client, basis: str, pad: str, layout: str) -> str:
    scheider = "&" if "?" in pad else "?"
    r = client.get(f"{basis}{pad}{scheider}layout={layout}")
    r.raise_for_status()
    return r.text


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
        regels.extend(f"  WEG      {label:11s} {weg}" for weg in sorted(a - b))
        regels.extend(f"  NIEUW    {label:11s} {erbij}" for erbij in sorted(b - a))
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
            oud = meet(haal_op(client, args.base, pad, "roos"))
            nieuw = meet(haal_op(client, args.base, pad, "nldd"))
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

    pad = TABPAGINA.format(project=project)
    if not args.only or args.only in pad:
        oud = meet(haal_op(client, args.base, pad, "roos"))
        nieuw = verenig([meet(haal_op(client, args.base, f"{pad}?tab={t}", "nldd")) for t in TABBLADEN])
        regels = vergelijk(oud, nieuw)
        weg = [r for r in regels if r.startswith("  WEG")]
        totaal += len(weg)
        print(f"{pad}  (alle tabbladen samen; {len(weg)} weg, {len(regels) - len(weg)} nieuw)")
        for r in regels:
            print(r)
        print()

    print(f"TOTAAL VERDWENEN GEDRAG: {totaal}")
    return 1 if totaal else 0


if __name__ == "__main__":
    sys.exit(main())
