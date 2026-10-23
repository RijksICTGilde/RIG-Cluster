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

from security_tests.sandboxlog import Record, junit_sleutel, route_uit, waarom

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
}


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
        bestand = (geval.get("classname") or "").split(".")[-1]
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


def dekkingstabel() -> list[str]:
    """Per doel uit de BIV-inventaris welke proef het beproeft."""
    per_component: dict[str, list[str]] = {}
    for naam, proef in PROEVEN.items():
        for component in proef.componenten:
            per_component.setdefault(component, []).append(naam)
    regels = ["| component | beproefd door |", "|---|---|"]
    for doel in aanvalsproef_doelen(load_inventaris()):
        proeven = ", ".join(sorted(per_component.get(doel, []))) or "nog niet beproefd"
        regels.append(f"| {doel} | {proeven} |")
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
) -> str:
    """Het rapport als markdown."""
    nu = datetime.datetime.now(tz=datetime.UTC).isoformat(timespec="seconds")
    regels = [
        "# Aanvalsproef ZAD Attack Proxy",
        "",
        f"Gedraaid op {nu} tegen de eigen wegwerp-sandbox.",
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
    regels += ["## Dekking van de aanvalsproef-doelen", "", *dekkingstabel(), ""]
    return "\n".join(regels)
