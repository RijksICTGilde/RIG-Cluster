"""Het doel van de aanvalsproef, en de grendel die een verkeerd doel weigert.

Het harnas leest zijn doel uit het runbestand dat ``task security-proof:up`` schrijft, en
nergens anders: een URL uit de omgeving of van de commandoregel komt er niet in. Daarbovenop
staan twee controles, want een runbestand is te bewerken: de host moet lokaal zijn, en de
instantie moet het token van dit runbestand terugkaatsen. Zo kan de proef alleen de sandbox
raken die hij zelf heeft opgezet.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

#: De map van het harnas buiten de Python-boom: nagebootste binaries, baselines, rapport.
PROEF_ROOT = Path(__file__).resolve().parents[2] / "security-proof"

#: Runtime-staat van een draaiende proefsandbox. Git-ignored, want er staan wegwerpsleutels in.
RUNBESTAND = PROEF_ROOT / ".proef" / "sandbox.json"

#: Het logboek waarin de nagebootste binaries hun aanroepen schrijven.
AANROEPLOG = PROEF_ROOT / ".proef" / "aanroepen.jsonl"

#: Het logboek van OPI zelf tijdens de proef. Apart van ``.proef/sandbox.log`` (de stdout die
#: ``task security-proof:up`` opvangt), zodat het in beide draaiwijzen dezelfde vorm heeft.
OPILOG = PROEF_ROOT / ".proef" / "opi.log"

#: Waar het rapport van een run landt.
RAPPORTMAP = PROEF_ROOT / "report"

#: De header waarmee de proefsandbox zich legitimeert.
TOKEN_HEADER = "X-Zad-Proef"

#: Alleen een instantie op deze host mag worden beproefd. Alles daarbuiten is een directe
#: afbreek: een aanvalsproef tegen iemand anders zijn omgeving is geen proef maar een aanval.
TOEGESTANE_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


class OnveiligDoel(Exception):
    """Het doel is niet de sandbox die dit harnas zelf heeft opgezet."""


@dataclass(frozen=True)
class Doel:
    """Een beproefbare sandbox: waar hij staat, en waarmee je er binnenkomt."""

    url: str
    token: str
    pid: int
    sleutels: dict[str, str]
    sessies: dict[str, str]
    aanroeplog: Path

    @property
    def projecten(self) -> list[str]:
        """De proefprojecten, op naam, in een vaste volgorde."""
        return sorted(self.sleutels)


def host_is_lokaal(url: str) -> bool:
    """Of deze URL naar de eigen machine wijst. De enige vraag die zonder verzoek te stellen is."""
    return (urlparse(url).hostname or "") in TOEGESTANE_HOSTS


def lees_runbestand(pad: Path = RUNBESTAND) -> Doel:
    """Het doel uit het runbestand, zonder het te verifieren."""
    if not pad.exists():
        raise OnveiligDoel(f"geen draaiende proefsandbox: {pad} bestaat niet (task security-proof:up)")
    gegevens = json.loads(pad.read_text())
    return Doel(
        url=str(gegevens["url"]).rstrip("/"),
        token=str(gegevens["token"]),
        pid=int(gegevens["pid"]),
        sleutels=dict(gegevens["sleutels"]),
        sessies=dict(gegevens["sessies"]),
        aanroeplog=Path(gegevens.get("aanroeplog", AANROEPLOG)),
    )


def verifieer_host(url: str) -> None:
    """Weiger een doel buiten de eigen machine, zonder er een verzoek naartoe te sturen."""
    if not host_is_lokaal(url):
        raise OnveiligDoel(
            f"doel {url!r} is geen lokale sandbox; dit harnas draait alleen tegen een instantie"
            f" die het zelf opzet (toegestaan: {', '.join(sorted(TOEGESTANE_HOSTS))})"
        )


def verifieer_doel(url: str, token: str) -> None:
    """Weiger elk doel dat niet de zelf opgezette sandbox is.

    De hostcontrole gaat voorop, zodat een extern doel wordt afgewezen zonder dat er een
    verzoek naartoe gaat.
    """
    verifieer_host(url)
    try:
        antwoord = httpx.get(f"{url.rstrip('/')}/health", timeout=5.0)
    except httpx.HTTPError as fout:
        raise OnveiligDoel(f"doel {url!r} antwoordt niet: {fout}") from fout
    gezien = antwoord.headers.get(TOKEN_HEADER, "")
    if gezien != token:
        raise OnveiligDoel(
            f"doel {url!r} kaatst token {gezien!r} terug in plaats van het token van dit runbestand;"
            " dit is niet de sandbox die het harnas heeft opgezet"
        )


def huidig_doel(pad: Path = RUNBESTAND) -> Doel:
    """Het geverifieerde doel. De enige weg waarlangs een proef een URL krijgt."""
    doel = lees_runbestand(pad)
    verifieer_doel(doel.url, doel.token)
    return doel


def main(argv: list[str] | None = None) -> int:
    """Grendel als commando: niet-nul exit en een melding zodra het doel niet mag."""
    argumenten = sys.argv[1:] if argv is None else argv
    try:
        if argumenten:
            verifieer_host(argumenten[0])
        doel = huidig_doel()
    except OnveiligDoel as fout:
        print(f"GEWEIGERD: {fout}", file=sys.stderr)
        return 2
    print(f"OK: {doel.url} is de eigen proefsandbox")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
