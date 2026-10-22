"""De uitkomst van een scanner tegen een ingecheckte uitzonderingslijst toetsen.

De aanvalsproef draait meerdere scanners: nuclei tegen een zelf opgezet doelwit,
kube-linter en checkov tegen de manifests. Elke scanner heeft zijn eigen uitvoerformaat,
maar dezelfde poort: een bevinding die niet in ``aanvalsproef/uitzonderingen.yaml`` staat
laat de run falen. Een uitzondering heeft een reden en een vervaldatum, net als in
``.trivyignore.yaml``, en een verlopen uitzondering zwijgt niet meer, dus de bevinding
komt vanzelf terug.

Gebruik::

    uv run python -m opi.tools.aanvalsproef grendel http://127.0.0.1:8111
    uv run python -m opi.tools.aanvalsproef nuclei nuclei.jsonl --drempel medium
    uv run python -m opi.tools.aanvalsproef kube-linter kube-linter.json
    uv run python -m opi.tools.aanvalsproef checkov checkov.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import yaml

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

#: Repo-root: vier mappen boven dit bestand (opi/tools -> opi -> python -> operations-manager).
REPO_ROOT = Path(__file__).resolve().parents[4]
UITZONDERINGEN = REPO_ROOT / "aanvalsproef" / "uitzonderingen.yaml"

#: De ernstniveaus van nuclei, van laag naar hoog. De drempel kiest er een uit.
ERNST_OPLOPEND = ("info", "low", "medium", "high", "critical")

#: Namen die als loopback gelden. Alles daarbuiten is geen zelf opgezet doelwit.
LOOPBACK_NAMEN = ("localhost", "127.0.0.1", "::1")


class Grendelfout(Exception):
    """Het doelwit is geen zelf opgezette instantie."""


@dataclass(frozen=True)
class Bevinding:
    """Een bevinding van een scanner, los van het formaat waarin hij stond."""

    id: str
    plek: str
    ernst: str


def grendel(doelwit: str) -> None:
    """Weiger elk doelwit dat niet op de loopback van deze runner staat.

    De aanvalsproef vuurt echt aanvalsverkeer af. Alleen een instantie die de run zelf
    heeft opgezet mag dat ontvangen, nooit productie en nooit de gedeelde sandbox.
    """
    ontleed = urlparse(doelwit)
    if ontleed.scheme not in ("http", "https"):
        raise Grendelfout(f"doelwit {doelwit!r} heeft geen http(s)-schema")
    host = ontleed.hostname
    if not host:
        raise Grendelfout(f"doelwit {doelwit!r} noemt geen host")
    if host in LOOPBACK_NAMEN:
        return
    try:
        adres = ip_address(host)
    except ValueError as fout:
        raise Grendelfout(f"doelwit {host!r} is geen loopback-adres en geen loopback-naam") from fout
    if not adres.is_loopback:
        raise Grendelfout(f"doelwit {host!r} is geen loopback-adres")


def geldige_uitzonderingen(soort: str, vandaag: date, pad: Path = UITZONDERINGEN) -> set[str]:
    """De id's die voor deze scanner zwijgen, zonder de verlopen en de onvolledige regels."""
    inhoud = yaml.safe_load(pad.read_text(encoding="utf-8")) or {}
    geldig = set()
    for regel in inhoud.get(soort) or []:
        vervalt = regel.get("expired_at")
        if not isinstance(vervalt, date) or not str(regel.get("statement") or "").strip():
            continue
        if vervalt >= vandaag:
            geldig.add(str(regel["id"]))
    return geldig


def lees_nuclei(rapport: Path) -> list[Bevinding]:
    """De bevindingen uit de JSONL-uitvoer van nuclei (``-jsonl-export``)."""
    bevindingen = []
    for regel in rapport.read_text(encoding="utf-8").splitlines():
        if not regel.strip():
            continue
        treffer = json.loads(regel)
        bevindingen.append(
            Bevinding(
                id=str(treffer.get("template-id", "<zonder template-id>")),
                plek=str(treffer.get("matched-at") or treffer.get("host") or ""),
                ernst=str((treffer.get("info") or {}).get("severity", "unknown")).lower(),
            )
        )
    return bevindingen


def lees_kube_linter(rapport: Path) -> list[Bevinding]:
    """De bevindingen uit ``kube-linter lint --format json``.

    kube-linter geeft geen ernst mee: de selectie in ``aanvalsproef/kube-linter.yaml`` is
    de drempel, dus elke bevinding telt even zwaar.
    """
    inhoud = json.loads(rapport.read_text(encoding="utf-8")) or {}
    bevindingen = []
    for melding in inhoud.get("Reports") or []:
        voorwerp = melding.get("Object") or {}
        k8s = voorwerp.get("K8sObject") or {}
        soort = (k8s.get("GroupVersionKind") or {}).get("Kind", "?")
        bestand = (voorwerp.get("Metadata") or {}).get("FilePath", "?")
        bevindingen.append(
            Bevinding(
                id=str(melding.get("Check", "<zonder check>")),
                plek=f"{soort}/{k8s.get('Name', '?')} in {bestand}",
                ernst="high",
            )
        )
    return bevindingen


def lees_checkov(rapport: Path) -> list[Bevinding]:
    """De gefaalde checks uit ``checkov -o json``.

    Checkov schrijft een lijst zodra er meerdere frameworks draaien, en anders een object.
    """
    inhoud = json.loads(rapport.read_text(encoding="utf-8"))
    rapporten = inhoud if isinstance(inhoud, list) else [inhoud]
    return [
        Bevinding(
            id=str(check.get("check_id", "<zonder check_id>")),
            plek=f"{check.get('resource', '?')} in {check.get('file_path', '?')}",
            ernst=str(check.get("severity") or "high").lower(),
        )
        for deel in rapporten
        for check in ((deel.get("results") or {}).get("failed_checks") or [])
    ]


def lees_semgrep(rapport: Path) -> list[Bevinding]:
    """De bevindingen uit ``semgrep scan --json``.

    De ernst van semgrep (INFO, WARNING, ERROR) wordt op de schaal van de drempel gelegd.
    """
    ernst_per_niveau = {"INFO": "low", "WARNING": "medium", "ERROR": "high"}
    inhoud = json.loads(rapport.read_text(encoding="utf-8")) or {}
    return [
        Bevinding(
            id=str(treffer.get("check_id", "<zonder check_id>")),
            plek=f"{treffer.get('path', '?')}:{(treffer.get('start') or {}).get('line', '?')}",
            ernst=ernst_per_niveau.get(str((treffer.get("extra") or {}).get("severity", "")), "high"),
        )
        for treffer in inhoud.get("results") or []
    ]


#: De lezers per scanner. De sleutel is ook de sectie in de uitzonderingslijst.
LEZERS: dict[str, Callable[[Path], list[Bevinding]]] = {
    "nuclei": lees_nuclei,
    "kube-linter": lees_kube_linter,
    "checkov": lees_checkov,
    "semgrep": lees_semgrep,
}


def boven_drempel(bevindingen: Iterable[Bevinding], drempel: str) -> list[Bevinding]:
    """De bevindingen met een ernst vanaf de drempel. Een onbekende ernst telt altijd mee."""
    grens = ERNST_OPLOPEND.index(drempel)
    return [
        bevinding
        for bevinding in bevindingen
        if bevinding.ernst not in ERNST_OPLOPEND or ERNST_OPLOPEND.index(bevinding.ernst) >= grens
    ]


def gate(bevindingen: Iterable[Bevinding], uitgezonderd: set[str], drempel: str) -> list[Bevinding]:
    """Wat er na drempel en uitzonderingen overblijft, en dus de run rood maakt."""
    return [bevinding for bevinding in boven_drempel(bevindingen, drempel) if bevinding.id not in uitgezonderd]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Toets de uitkomst van een aanvalsproef-scanner.")
    parser.add_argument("soort", choices=["grendel", *LEZERS], help="de scanner, of grendel voor de doelwitcontrole")
    parser.add_argument("doelwit", help="het rapportbestand, of de URL bij grendel")
    parser.add_argument(
        "--drempel", choices=ERNST_OPLOPEND, default="medium", help="vanaf welke ernst een bevinding meetelt"
    )
    parser.add_argument("--uitzonderingen", type=Path, default=UITZONDERINGEN, help="pad naar de uitzonderingslijst")
    args = parser.parse_args(argv)

    if args.soort == "grendel":
        try:
            grendel(args.doelwit)
        except Grendelfout as fout:
            sys.stderr.write(f"grendel: {fout}\n")
            return 1
        sys.stdout.write(f"grendel: {args.doelwit} is een zelf opgezet doelwit\n")
        return 0

    rapport = Path(args.doelwit)
    if not rapport.exists():
        sys.stderr.write(f"rapport {rapport} bestaat niet, de scanner heeft niets geschreven\n")
        return 1

    bevindingen = LEZERS[args.soort](rapport)
    uitgezonderd = geldige_uitzonderingen(args.soort, datetime.now(tz=UTC).date(), args.uitzonderingen)
    nieuw = gate(bevindingen, uitgezonderd, args.drempel)

    sys.stdout.write(
        f"{args.soort}: {len(bevindingen)} bevindingen, drempel {args.drempel},"
        f" {len(uitgezonderd)} geldige uitzonderingen, {len(nieuw)} blokkerend\n"
    )
    for bevinding in nieuw:
        sys.stderr.write(f"  {bevinding.ernst:>8}  {bevinding.id}  {bevinding.plek}\n")
    if nieuw:
        sys.stderr.write(
            "Nieuwe bevindingen boven de drempel. Dicht ze, of zet ze met een reden en een"
            f" vervaldatum in {args.uitzonderingen}.\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
