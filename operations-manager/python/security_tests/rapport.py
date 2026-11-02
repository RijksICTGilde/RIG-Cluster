"""Het rapport van een aanvalsproef: wat er gemeten is, wat er uitkwam, en wat het raakt.

Een bevinding is pas bruikbaar als er een reproductiepad bij staat en als duidelijk is welke
vertrouwensgrens hij raakt. De koppeling naar de BIV-as en de componenten komt uit
``PROEVEN``, en de doelen zelf uit dezelfde inventaris als ``architecture/biv-inventaris.yaml``:
een component met score H die deze proef nog niet dekt, staat in het rapport als gat.
"""

from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING, NamedTuple

from opi.tools.biv_render import aanvalsproef_doelen, load_inventaris

from security_tests.baseline import lees_baseline
from security_tests.doel import LOKAAL, SANDBOX
from security_tests.sandboxlog import Record, bestand_van, junit_sleutel, route_uit, toetsnaam, waarom
from security_tests.zap_scan import proefnaam

if TYPE_CHECKING:
    from pathlib import Path

    from security_tests.sandboxlog import AsgiFout
    from security_tests.zap_scan import ZapUitkomst


class Telling(NamedTuple):
    """Hoeveel toetsen het junit-rapport bevat, en hoeveel ervan echt iets gemeten hebben."""

    toetsen: int
    overgeslagen: int

    @property
    def gemeten(self) -> int:
        return self.toetsen - self.overgeslagen


class Proef(NamedTuple):
    """Wat een proefbestand beproeft, en waar dat in de inventaris op neerkomt."""

    as_naam: str
    componenten: tuple[str, ...]
    impact: str
    doelen: tuple[str, ...] = (LOKAAL, SANDBOX)


#: Per proefbestand de BIV-as en de componenten die hij raakt. Dit is de enige plek waar die
#: koppeling staat; het rapport en de dekkingstabel lezen hem allebei hier.
PROEVEN: dict[str, Proef] = {
    "test_grendel": Proef("integriteit", ("opi",), "de proef kan alleen de eigen sandbox raken"),
    "test_auth_dekking": Proef("vertrouwelijkheid", ("opi",), "een route zonder herkenning stelt projectgegevens open"),
    "test_uploads": Proef("beschikbaarheid", ("opi",), "een upload die in het geheugen landt legt de pod plat"),
    "test_stortvloed": Proef(
        "beschikbaarheid", ("opi",), "een stortvloed of het gokken van een sleutel kost de aanvaller niets"
    ),
    "test_injectie": Proef(
        "integriteit",
        ("gitops-repositories", "tenant-namespaces"),
        "invoer op een argumentenlijst voert iets uit buiten het eigen project",
    ),
    "test_web_payloads": Proef(
        "vertrouwelijkheid", ("opi",), "een lek of een CSRF neemt de sessie van een beheerder over"
    ),
    "test_schemathesis": Proef(
        "vertrouwelijkheid", ("opi",), "een operatie zonder grendel geeft een vreemde toegang tot een project"
    ),
    "test_zap": Proef("vertrouwelijkheid", ("opi",), "de bekende webkwetsbaarheden van OWASP"),
    "test_sql_injectie": Proef(
        "vertrouwelijkheid",
        ("opi", "gedeelde-postgresql"),
        "een injectie in een filter leest taken van een ander project",
    ),
    "test_sandboxdoel": Proef(
        "vertrouwelijkheid",
        ("keycloak", "object-storage", "registry", "argocd", "tenant-namespaces"),
        "een tenant of een vreemde komt bij een dienst van een ander project",
        doelen=(SANDBOX,),
    ),
}

#: De lagen in de groene lijst. Alles wat niet in deze tabel staat is een gerichte proef.
LAAG_PER_PROEF = {"test_schemathesis": "schemathesis", "test_zap": "ZAP-lezing"}

#: De uitkomsten die een regel kan hebben. Elke regel heeft er een: een regel zonder uitkomst
#: zou precies de stille waarschuwing zijn die de groene lijst uitsluit. ``INFORMATIE`` is de
#: uitkomst van een ZAP-alert onder de risicodrempel, die geen uitspraak over de gate doet.
GROEN = "groen"
AFWIJKING = "afwijking met uitleg"
BEVINDING_REGEL = "bevinding"
NIET_GEMETEN = "niet gemeten"
INFORMATIE = "informatie"


class Regel(NamedTuple):
    """Een gemeten regel in de groene lijst: wat er gemeten is, en wat eruit kwam."""

    laag: str
    regel: str
    uitkomst: str
    toelichting: str = ""


@dataclass(frozen=True)
class Bevinding:
    """Een bevinding met alles wat een lezer nodig heeft om hem na te doen.

    ``logregels`` en ``route`` zeggen WAAROM OPI zwichtte: de assertie van de proef alleen
    vertelt wat er niet klopte, niet wat er in OPI gebeurde.
    """

    proef: str
    naam: str
    melding: str
    reproductie: str
    route: str = ""
    logregels: tuple[str, ...] = ()

    @property
    def details(self) -> Proef | None:
        return PROEVEN.get(self.proef)


@dataclass(frozen=True)
class Crash:
    """Een proef die niet op een uitslag eindigde maar op een exitcode."""

    exitcode: int
    laatste_regels: tuple[str, ...]


def uit_junit(pad: Path, vensters: dict[str, list[Record]] | None = None) -> list[Bevinding]:
    """De bevindingen uit het junit-rapport van de fuzzers, met hun OPI-logregels erbij."""
    boom = ET.parse(pad)  # noqa: S314 - ons eigen junit-rapport uit dezelfde run
    bevindingen = []
    for geval in boom.iter("testcase"):
        mislukt = [kind for kind in geval if kind.tag in ("failure", "error")]
        if not mislukt:
            continue
        bestand = bestand_van(geval.get("classname") or "")
        naam = geval.get("name") or "?"
        melding = (mislukt[0].get("message") or mislukt[0].text or "").strip()
        venster = (vensters or {}).get(junit_sleutel(geval.get("classname") or "", naam), [])
        bevindingen.append(
            Bevinding(
                proef=bestand,
                naam=naam,
                melding=melding,
                reproductie=f"uv run pytest security_tests/{bestand}.py -k {naam.split('[')[0]!r}",
                route=route_uit(venster) if venster else "",
                logregels=waarom(venster),
            )
        )
    return bevindingen


def telling_uit_junit(pad: Path) -> Telling:
    """De aantallen uit het junit-rapport.

    De gate leest dit naast de bevindingen: slaat alles over (bijvoorbeeld omdat de grendel
    het doel niet herkende), dan staan er geen failures in het rapport en zou de gate groen
    eindigen zonder iets te hebben beproefd.
    """
    if not pad.exists():
        return Telling(0, 0)
    boom = ET.parse(pad)  # noqa: S314 - ons eigen junit-rapport uit dezelfde run
    wortel = boom.getroot()
    suites = list(wortel.iter("testsuite")) or [wortel]
    return Telling(
        toetsen=sum(int(suite.get("tests") or 0) for suite in suites),
        overgeslagen=sum(int(suite.get("skipped") or 0) for suite in suites),
    )


def laag_van(proefbestand: str) -> str:
    """De laag waarin deze proef in de groene lijst staat."""
    return LAAG_PER_PROEF.get(proefbestand, "gerichte proeven")


def regels_uit_junit(pad: Path) -> list[Regel]:
    """Elke toets uit het junit-rapport als regel in de groene lijst.

    Een overgeslagen toets is "niet gemeten" met de reden van de skip, niet groen: dat is het
    verschil tussen een proef die iets heeft beproefd en een proef die er niet aan toe kwam.
    """
    if not pad.exists():
        return []
    boom = ET.parse(pad)  # noqa: S314 - ons eigen junit-rapport uit dezelfde run
    regels = []
    for geval in boom.iter("testcase"):
        bestand = bestand_van(geval.get("classname") or "")
        naam = geval.get("name") or "?"
        mislukt = [kind for kind in geval if kind.tag in ("failure", "error")]
        overgeslagen = [kind for kind in geval if kind.tag == "skipped"]
        if mislukt:
            uitkomst, toelichting = BEVINDING_REGEL, (mislukt[0].get("message") or "").splitlines()[:1]
        elif overgeslagen:
            uitkomst, toelichting = NIET_GEMETEN, (overgeslagen[0].get("message") or "").splitlines()[:1]
        else:
            uitkomst, toelichting = GROEN, []
        regel = toetsnaam(geval.get("classname") or "", naam)
        regels.append(Regel(laag_van(bestand), regel, uitkomst, " ".join(toelichting)))
    return regels


def regels_uit_zap(zap: ZapUitkomst, doelsoort: str = "") -> list[Regel]:
    """De ZAP-alerts als regels: elke WARN met zijn uitleg, en de informatie eronder.

    Een WARN met een reden in de baseline is een afwijking met uitleg; een WARN zonder reden is
    een bevinding. Zo staat geen enkele alert van ZAP nog buiten de lijst.
    """
    if not zap.gemeten:
        return [Regel("ZAP", "alle passen", NIET_GEMETEN, zap.reden)]
    regels = []
    for sleutel, melding in sorted(zap.bevindingen.items()):
        pas, _, kenmerk = sleutel.partition("/")
        reden = lees_baseline(proefnaam(pas), doelsoort).get(kenmerk, "")
        uitkomst = AFWIJKING if reden else BEVINDING_REGEL
        regels.append(Regel("ZAP", f"{pas} {kenmerk}", uitkomst, reden or melding))
    regels += [
        Regel("ZAP", f"{sleutel.replace('/', ' ', 1)}", INFORMATIE, melding)
        for sleutel, melding in sorted(zap.informatie.items())
    ]
    # Een uitleg die niets meer dekt laat de run falen, dus hij hoort in de lijst te staan.
    regels += [
        Regel("ZAP", sleutel.replace("/", " ", 1), BEVINDING_REGEL, f"OPGELOST, dekt niets meer: {reden}")
        for sleutel, reden in sorted(zap.verouderd.items())
    ]
    # Een pas zonder herkenning op dit doel heeft niets beproefd. Dat is geen groen en geen
    # bevinding, dus staat hij met zijn reden als NIET GEMETEN in de lijst.
    regels += [
        Regel("ZAP", f"{pas} alle regels", NIET_GEMETEN, reden) for pas, reden in sorted(zap.niet_gemeten.items())
    ]
    return regels


def groene_lijst(regels: list[Regel]) -> list[str]:
    """De groene lijst: per laag elke gemeten regel met zijn uitkomst."""
    uitvoer = [
        "## Groene lijst",
        "",
        "Per laag elke regel die gemeten is. Een afwijking staat erbij met de reden uit"
        " `bekende-bevindingen.tsv`; een bevinding en een regel die niets meer dekt laten de"
        " run falen.",
        "",
    ]
    for laag in sorted({regel.laag for regel in regels}):
        van_de_laag = [regel for regel in regels if regel.laag == laag]
        groen = len([regel for regel in van_de_laag if regel.uitkomst == GROEN])
        uitvoer += [
            f"### {laag} ({groen} groen van {len(van_de_laag)})",
            "",
            "| regel | uitkomst | toelichting |",
            "|---|---|---|",
            *(f"| {regel.regel} | {regel.uitkomst} | {regel.toelichting or '-'} |" for regel in van_de_laag),
            "",
        ]
    return uitvoer


def dekkingstabel(doelsoort: str = "") -> list[str]:
    """Per doel uit de BIV-inventaris welke proef het beproeft, en tegen welk doel."""
    per_component: dict[str, list[str]] = {}
    doelen_per_component: dict[str, set[str]] = {}
    for naam, proef in PROEVEN.items():
        for component in proef.componenten:
            per_component.setdefault(component, []).append(naam)
            doelen_per_component.setdefault(component, set()).update(proef.doelen)
    regels = [
        f"Deze run beproefde het doel: {doelsoort or 'onbekend'}. Staging is een handrun met"
        " hetzelfde harnas; zie features/aanvalsproef-staging.md.",
        "",
        "| component | beproefd door | beproefbaar tegen |",
        "|---|---|---|",
    ]
    for doel in aanvalsproef_doelen(load_inventaris()):
        proeven = ", ".join(sorted(per_component.get(doel, []))) or "nog niet beproefd"
        doelen = ", ".join(sorted(doelen_per_component.get(doel, set()))) or "-"
        regels.append(f"| {doel} | {proeven} | {doelen} |")
    return regels


def _waarom_regels(bevinding: Bevinding) -> list[str]:
    """Het WAAROM van een bevinding: de route en de OPI-logregels eromheen."""
    if not bevinding.logregels:
        return ["- waarom OPI zwichtte: OPI logde niets in het venster van deze proef"]
    regels = [f"- route: {bevinding.route or 'onbekend'}", "- waarom OPI zwichtte:", "", "```"]
    regels += list(bevinding.logregels)
    regels += ["```", ""]
    return regels


def _asgi_sectie(fouten: list[AsgiFout]) -> list[str]:
    """Per ``Exception in ASGI application`` het oordeel: bevinding of verwachte uitkomst."""
    if not fouten:
        return []
    bevindingen = [fout for fout in fouten if not fout.verwacht]
    regels = [
        "## Exception in ASGI application",
        "",
        f"{len(fouten)} keer in deze run, waarvan {len(bevindingen)} een bevinding en"
        f" {len(fouten) - len(bevindingen)} de verwachte uitkomst van een wegwerp-sandbox"
        " zonder database.",
        "",
    ]
    # Gegroepeerd op route, oorzaak en proef: de stortvloedproef levert dezelfde fout tien
    # keer op, en tien identieke regels maken het rapport onleesbaar.
    for (route, oorzaken, proef, oordeel, reden), tijden in _groepen(fouten).items():
        wanneer = tijden[0] if len(tijden) == 1 else f"{len(tijden)}x, {tijden[0]} tot {tijden[-1]}"
        regels.append(f"- {wanneer} `{route}` ({oorzaken or 'geen traceback'}) in {proef}: {oordeel}, {reden}")
    return [*regels, ""]


def _groepen(fouten: list[AsgiFout]) -> dict[tuple[str, str, str, str, str], list[str]]:
    """De ASGI-fouten die dezelfde route, oorzaak en proef delen, met hun tijden."""
    per_groep: dict[tuple[str, str, str, str, str], list[str]] = {}
    for fout in fouten:
        sleutel = (fout.route, ", ".join(fout.oorzaken), fout.proef, fout.oordeel, fout.reden)
        per_groep.setdefault(sleutel, []).append(fout.tijd)
    return per_groep


def _crashsectie(crash: Crash | None) -> list[str]:
    """Een proef die zelf omviel: de exitcode en de laatste logregels van OPI."""
    if crash is None:
        return []
    return [
        "## Crash van de proef zelf",
        "",
        f"pytest eindigde op exitcode {crash.exitcode}, dus niet op een uitslag. De laatste"
        " logregels van OPI voor dat moment:",
        "",
        "```",
        *(crash.laatste_regels or ("geen logregels opgevangen",)),
        "```",
        "",
    ]


def render(
    bevindingen: list[Bevinding],
    zap: ZapUitkomst,
    telling: Telling,
    asgi: list[AsgiFout] | None = None,
    crash: Crash | None = None,
    lijst: list[Regel] | None = None,
    doelsoort: str = "",
) -> str:
    """Het rapport als markdown."""
    nu = datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds")
    regels = [
        "# Aanvalsproef ZAD Attack Proxy",
        "",
        f"Gedraaid op {nu} tegen doel: {doelsoort or 'onbekend'}.",
        "",
        "## Uitkomst",
        "",
        f"- gerichte proeven: {telling.gemeten} gemeten van {telling.toetsen} ({telling.overgeslagen} overgeslagen)",
        f"- bevindingen uit de gerichte proeven: {len(bevindingen)}",
        f"- Exception in ASGI application: {len(asgi or [])}, waarvan"
        f" {len([fout for fout in asgi or [] if not fout.verwacht])} een bevinding",
        f"- ZAP-basislaag: {'gemeten, ' + str(len(zap.meldingen)) + ' meldingen' if zap.gemeten else 'NIET GEMETEN: ' + zap.reden}",
        "",
    ]
    if bevindingen:
        regels += ["## Bevindingen", ""]
        for bevinding in bevindingen:
            details = bevinding.details
            regels += [
                f"### {bevinding.proef}: {bevinding.naam}",
                "",
                f"- wat: {bevinding.melding.splitlines()[0] if bevinding.melding else 'geen melding'}",
                f"- BIV-as: {details.as_naam if details else 'onbekend'}",
                f"- raakt: {', '.join(details.componenten) if details else 'onbekend'}",
                f"- impact: {details.impact if details else 'onbekend'}",
                f"- reproductie: `{bevinding.reproductie}`",
                *_waarom_regels(bevinding),
                "",
            ]
    regels += _crashsectie(crash)
    regels += _asgi_sectie(asgi or [])
    if zap.meldingen:
        regels += ["## ZAP", "", *(f"- {melding}" for melding in zap.meldingen), ""]
    regels += [*groene_lijst(lijst or []), ""] if lijst else []
    regels += ["## Dekking van de aanvalsproef-doelen", "", *dekkingstabel(doelsoort), ""]
    return "\n".join(regels)
