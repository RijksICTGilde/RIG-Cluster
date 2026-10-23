"""De ZAP-basislaag: OWASP ZAP tegen de eigen proefsandbox.

ZAP draait als container, dus deze module bouwt het commando en leest het JSON-rapport; hij
doet zelf geen HTTP. Drie passen: ongeauthenticeerd, als projectlid en als ZAD-admin, elk met
de OpenAPI als zaad zodat ook de machine-API wordt geraakt.

Zonder docker is er niets te draaien. Dan meldt deze module dat het niet gemeten is, in plaats
van groen te zeggen: een proef die stil overslaat is erger dan geen proef.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from security_tests.baseline import toets_tegen_baseline
from security_tests.doel import RAPPORTMAP, TOKEN_HEADER

if TYPE_CHECKING:
    from pathlib import Path

    from security_tests.doel import Doel

PROEF = "zap"

#: Het ZAP-image, op de bewegende stabiele tag van het project zelf. Geen digest-pin: dit
#: is lokaal gereedschap en een vaste digest zou elke ZAP-uitgave met de hand vragen.
ZAP_IMAGE = "ghcr.io/zaproxy/zaproxy:stable"

#: Vanaf welk risiconiveau een bevinding meetelt. ZAP's riskcode: 0 info, 1 laag, 2 midden, 3 hoog.
RISICODREMPEL = 2

#: Hoe lang een pas mag duren voordat we hem afbreken.
TIJDBUDGET_SECONDEN = 900

#: Hoe lang de tokenproef mag duren. Die doet één verzoek.
TIJDBUDGET_PROEF_SECONDEN = 60

#: De naam waarmee een container op Docker Desktop (Mac, Windows) de host bereikt.
HOSTNAAM_IN_CONTAINER = "host.docker.internal"


@dataclass
class ZapUitkomst:
    """Wat een ZAP-ronde opleverde."""

    gemeten: bool
    reden: str = ""
    bevindingen: dict[str, str] = field(default_factory=dict)
    meldingen: list[str] = field(default_factory=list)


def zap_beschikbaar() -> tuple[bool, str]:
    """Of ZAP hier te draaien is, en zo niet: waarom niet."""
    if shutil.which("docker") is None:
        return False, "docker staat niet op het PATH; ZAP draait als container"
    return True, ""


def containerdoel(url: str) -> tuple[str, list[str]]:
    """De URL en netwerkopties waarmee de ZAP-container het doel bereikt.
    Zie README.md: Hoe de container bij de sandbox komt."""
    if sys.platform.startswith("linux"):
        return url, ["--network", "host"]
    adres = urlparse(url)
    netloc = HOSTNAAM_IN_CONTAINER if adres.port is None else f"{HOSTNAAM_IN_CONTAINER}:{adres.port}"
    return adres._replace(netloc=netloc).geturl(), []


def tokenproef(doel: Doel) -> list[str]:
    """Het docker-commando dat het proeftoken van het doel ophaalt.
    Een verbinding zegt alleen dat er iets luistert; het token bewijst dat het de sandbox is."""
    url, netwerk = containerdoel(doel.url)
    script = (
        "import sys, urllib.request;"
        f"antwoord = urllib.request.urlopen({url + '/health'!r}, timeout=5);"
        f"sys.exit(0 if antwoord.headers.get({TOKEN_HEADER!r}) == {doel.token!r} else 3)"
    )
    return ["docker", "run", "--rm", *netwerk, "--entrypoint", "python3", ZAP_IMAGE, "-c", script]


def verifieer_containerdoel(doel: Doel) -> str:
    """Leeg als de container de sandbox ziet, anders waarom niet.
    Loopt vóór de passen, zodat een onbereikbaar doel NIET GEMETEN oplevert."""
    url, _ = containerdoel(doel.url)
    try:
        uitkomst = subprocess.run(
            tokenproef(doel),
            capture_output=True,
            text=True,
            timeout=TIJDBUDGET_PROEF_SECONDEN,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as fout:
        return f"de tokenproef naar {url} kwam niet verder dan {fout}"
    if uitkomst.returncode == 0:
        return ""
    melding = (uitkomst.stderr or uitkomst.stdout).strip()[-300:].rstrip(".")
    return (
        f"de ZAP-container krijgt het proeftoken niet van {url}"
        f" (exit {uitkomst.returncode}): {melding or 'geen uitvoer'}"
    )


def _header_opties(naam: str, waarde: str) -> list[str]:
    """ZAP's replacer-configuratie om een header aan elk verzoek toe te voegen."""
    return [
        "-z",
        " ".join(
            [
                "-config replacer.full_list(0).description=herkenning",
                "-config replacer.full_list(0).enabled=true",
                "-config replacer.full_list(0).matchtype=REQ_HEADER",
                f"-config replacer.full_list(0).matchstr={naam}",
                f"-config replacer.full_list(0).replacement={waarde}",
                "-config replacer.full_list(0).initiators=",
            ]
        ),
    ]


def commando(doel: Doel, pas: str, rapport: Path) -> list[str]:
    """Het docker-commando voor een pas, met de weg naar het doel uit ``containerdoel``."""
    opties: list[str] = []
    if pas == "projectlid":
        opties = _header_opties("X-API-Key", doel.sleutels[doel.projecten[0]])
    elif pas == "zad-admin":
        opties = _header_opties("Cookie", f"session={doel.sessies['zad-admin']}")
    url, netwerk = containerdoel(doel.url)
    return [
        "docker",
        "run",
        "--rm",
        *netwerk,
        "--volume",
        f"{rapport.parent}:/zap/wrk",
        ZAP_IMAGE,
        "zap-api-scan.py",
        "-t",
        f"{url}/openapi.json",
        "-f",
        "openapi",
        "-J",
        rapport.name,
        "-I",
        *opties,
    ]


def lees_bevindingen(rapport: Path, drempel: int = RISICODREMPEL) -> dict[str, str]:
    """De bevindingen uit een ZAP-rapport vanaf de risicodrempel, als kenmerk naar melding."""
    inhoud = json.loads(rapport.read_text())
    bevindingen: dict[str, str] = {}
    for site in inhoud.get("site", []):
        for alarm in site.get("alerts", []):
            if int(alarm.get("riskcode", 0)) < drempel:
                continue
            kenmerk = f"{alarm.get('pluginid', '?')} {alarm.get('name', '?')}"
            urls = sorted({geval.get("uri", "") for geval in alarm.get("instances", [])})
            bevindingen[kenmerk] = f"risico {alarm.get('riskdesc', '?')} op {', '.join(urls[:3])}"
    return bevindingen


def draai(doel: Doel, passen: tuple[str, ...] = ("ongeauthenticeerd", "projectlid", "zad-admin")) -> ZapUitkomst:
    """Draai elke pas en toets de bevindingen tegen de baseline."""
    beschikbaar, reden = zap_beschikbaar()
    if not beschikbaar:
        return ZapUitkomst(gemeten=False, reden=reden)
    onbereikbaar = verifieer_containerdoel(doel)
    if onbereikbaar:
        return ZapUitkomst(gemeten=False, reden=onbereikbaar)

    RAPPORTMAP.mkdir(parents=True, exist_ok=True)
    # Dicht, ook als een eerdere versie deze map wereld-schrijfbaar achterliet.
    RAPPORTMAP.chmod(0o755)
    # Alleen deze map gaat open voor ZAP; gate-bestanden blijven erbuiten (sticky voor symlinks).
    zapmap = RAPPORTMAP / "zap"
    zapmap.mkdir(parents=True, exist_ok=True)
    zapmap.chmod(0o1777)
    alles: dict[str, str] = {}
    for pas in passen:
        rapport = zapmap / f"zap-{pas}.json"
        log = RAPPORTMAP / f"zap-{pas}.log"
        # Elk rapport moet van deze pas zijn. Een oud rapport dat niet weg kan is NIET GEMETEN.
        try:
            rapport.unlink(missing_ok=True)
        except OSError as fout:
            return ZapUitkomst(
                gemeten=False,
                reden=f"ZAP-pas {pas}: het oude rapport {rapport} is niet weg te halen ({fout})",
            )
        try:
            uitkomst = subprocess.run(
                commando(doel, pas, rapport),
                capture_output=True,
                text=True,
                timeout=TIJDBUDGET_SECONDEN,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ZapUitkomst(gemeten=False, reden=f"ZAP-pas {pas} liep over {TIJDBUDGET_SECONDEN}s heen")
        # De hele uitvoer naar een bestand: ZAP schrijft zijn diagnose op stdout, en een
        # stacktrace uit zijn netwerklaag is in een melding van 500 tekens niet te lezen.
        log.write_text(uitkomst.stdout + uitkomst.stderr)
        if not rapport.exists():
            return ZapUitkomst(
                gemeten=False,
                reden=(
                    f"ZAP-pas {pas} schreef geen rapport (uitvoer: {log}); de container bereikte"
                    " het doel wel, dus ZAP brak op iets anders af"
                ),
            )
        alles.update({f"{pas}/{kenmerk}": melding for kenmerk, melding in lees_bevindingen(rapport).items()})
    return ZapUitkomst(gemeten=True, bevindingen=alles, meldingen=toets_tegen_baseline(PROEF, alles))
