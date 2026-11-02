"""Het doel van de aanvalsproef, en de grendel die een verkeerd doel weigert.

Het harnas leest zijn doel uit het runbestand dat ``task security-proof:up`` of
``task security-proof:sandbox`` schrijft, en nergens anders: een URL uit de omgeving of van de
commandoregel komt er niet in. Daarbovenop staan twee controles, want een runbestand is te
bewerken: de host staat op de vaste lijst met beproefbare hosts (de eigen machine, of een
sandbox op het sandboxdomein), en de instantie moet het token van dit runbestand terugkaatsen.

Er zijn dus precies twee soorten doel, en geen derde: de wegwerp-sandbox op de eigen machine,
en een geclaimde sandbox in het kind-cluster. Staging is een afgestemde handrun met zijn eigen
runbestand (zie ``features/aanvalsproef-staging.md``); productie nooit.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from opi.core.config import SECURITY_PROOF_HEADER

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

#: De header waarmee de proefsandbox zich legitimeert. Eén definitie, in OPI zelf, want een
#: proefdoel in het cluster kaatst het token met die naam terug.
TOKEN_HEADER = SECURITY_PROOF_HEADER

#: De eigen machine: de wegwerp-sandbox die het harnas zelf opzet.
LOKALE_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: Het domein van de sandboxcluster. Het enige doel buiten de eigen machine, en op naam: een
#: IP-adres buiten de loopback komt er dus niet door, en geen ander domein.
SANDBOXDOMEIN = "sandbox.rijksapp.dev"

#: De twee soorten doel, zoals ze in het rapport en de dekkingstabel staan.
LOKAAL = "lokaal"
SANDBOX = "sandbox"


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
    def soort(self) -> str:
        """Welk van de twee doelen dit is. Bepaalt wat er te meten valt, niet of het mag."""
        return soort_doel(self.url) or ""

    @property
    def projecten(self) -> list[str]:
        """De proefprojecten, op naam, in een vaste volgorde."""
        return sorted(self.sleutels)


def soort_doel(url: str) -> str | None:
    """Welk soort doel deze URL is, of None als hij niet beproefd mag worden."""
    host = (urlparse(url).hostname or "").lower()
    if host in LOKALE_HOSTS:
        return LOKAAL
    if host == SANDBOXDOMEIN or host.endswith(f".{SANDBOXDOMEIN}"):
        return SANDBOX
    return None


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
    """Weiger een doel buiten de beproefbare hosts, zonder er een verzoek naartoe te sturen."""
    if soort_doel(url) is None:
        raise OnveiligDoel(
            f"doel {url!r} is geen beproefbare sandbox; dit harnas draait alleen tegen de eigen"
            f" machine ({', '.join(sorted(LOKALE_HOSTS))}) of tegen een host op {SANDBOXDOMEIN}"
        )


def verifieer_doel(url: str, token: str) -> None:
    """Weiger elk doel dat niet de sandbox van deze run is.

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
            " dit is niet de sandbox van deze run"
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
    print(f"OK: {doel.url} is de proefsandbox van deze run ({doel.soort})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
