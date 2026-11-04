"""De ASVS-zelfbeoordeling lezen, toetsen en als afvinkdocument renderen.

``architecture/asvs-5.0-eisen.yaml`` is de meetlat: de ASVS-eisen op L1 en L2, letterlijk
uit de standaard. ``architecture/asvs-zelfbeoordeling.yaml`` is ons oordeel per eis, en
``docs/security/asvs-zelfbeoordeling.md`` volgt daaruit en wordt nooit met de hand bijgewerkt.

Wat deze toets bewijst: elke eis op het gekozen niveau heeft precies een oordeel, en een
oordeel dat naar een automatische toets wijst noemt een toets die echt bestaat. Een regel
die naar een testbestand of een workflow-job verwijst die er niet (meer) is, is geen
dekking maar een belofte.

Gebruik::

    uv run python -m opi.tools.asvs_render           # schrijf het afvinkdocument
    uv run python -m opi.tools.asvs_render --check    # exit 1 bij een gat of drift
    uv run python -m opi.tools.asvs_render --stdout   # print, schrijf niet
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

#: Repo-root: vier mappen boven dit bestand (opi/tools -> opi -> python -> operations-manager).
REPO_ROOT = Path(__file__).resolve().parents[4]
EISEN = REPO_ROOT / "architecture" / "asvs-5.0-eisen.yaml"
BEOORDELING = REPO_ROOT / "architecture" / "asvs-zelfbeoordeling.yaml"
AFVINKLIJST = REPO_ROOT / "docs" / "security" / "asvs-zelfbeoordeling.md"


class Status(StrEnum):
    """Hoe een eis is afgedekt."""

    TOETS = "toets"
    PLAN = "plan"
    HANDWERK = "handwerk"
    NVT = "nvt"


#: Hoe elke status in het document heet.
STATUSNAAM = {
    Status.TOETS: "automatische toets",
    Status.PLAN: "in een plan",
    Status.HANDWERK: "handwerk",
    Status.NVT: "niet van toepassing",
}


class Eis(BaseModel):
    """Een ASVS-eis, letterlijk uit de standaard."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^V\d+\.\d+\.\d+$")
    niveau: int
    kapittel: str
    sectie: str
    eis: str


class Oordeel(BaseModel):
    """Ons oordeel over een eis: waar de dekking zit, of dat hij handwerk blijft."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^V\d+\.\d+\.\d+$")
    status: Status
    toets: str | None = None
    plan: str | None = None
    eigenaar: str | None = None
    toelichting: str | None = None


def laad_eisen(pad: Path = EISEN) -> list[Eis]:
    inhoud = yaml.safe_load(pad.read_text(encoding="utf-8"))
    return [Eis(**eis) for eis in inhoud["eisen"]]


def laad_beoordeling(pad: Path = BEOORDELING) -> tuple[str, list[Oordeel]]:
    """Het gekozen niveau en de oordelen."""
    inhoud = yaml.safe_load(pad.read_text(encoding="utf-8"))
    return str(inhoud["niveau"]), [Oordeel(**regel) for regel in inhoud["oordelen"]]


def toets_bestaat(verwijzing: str, wortel: Path = REPO_ROOT) -> bool:
    """Of een genoemde toets bestaat.

    Een verwijzing is een pad (``operations-manager/python/tests/test_csrf_enforcement.py``)
    of een pad met een job erachter (``.github/workflows/security.yml#semgrep``). Bij een
    job wordt ook gekeken of die job in de workflow staat.
    """
    pad, _, job = verwijzing.partition("#")
    bestand = wortel / pad
    if not bestand.is_file():
        return False
    if not job:
        return True
    inhoud = yaml.safe_load(bestand.read_text(encoding="utf-8")) or {}
    return job in (inhoud.get("jobs") or {})


def meldingen(eisen: list[Eis], oordelen: list[Oordeel]) -> list[str]:
    """Wat er aan de beoordeling mankeert, leeg als ze volledig en onderbouwd is."""
    gevonden: list[str] = []
    per_id: dict[str, list[Oordeel]] = {}
    for oordeel in oordelen:
        per_id.setdefault(oordeel.id, []).append(oordeel)

    ids = {eis.id for eis in eisen}
    gevonden += [f"{eis_id}: geen oordeel, de eis is niet beoordeeld" for eis_id in sorted(ids - per_id.keys())]
    gevonden += [
        f"{eis_id}: oordeel over een eis die niet op dit niveau in de standaard staat"
        for eis_id in sorted(per_id.keys() - ids)
    ]
    gevonden += [
        f"{eis_id}: {len(regels)} oordelen, dat moet er een zijn"
        for eis_id, regels in sorted(per_id.items())
        if len(regels) > 1
    ]

    for oordeel in oordelen:
        gevonden += _bezwaren(oordeel)
    return gevonden


def _bezwaren(oordeel: Oordeel) -> list[str]:
    """Wat er mis is met een los oordeel."""
    if oordeel.status is Status.TOETS:
        if not oordeel.toets:
            return [f"{oordeel.id}: status toets zonder een genoemde toets"]
        ontbrekend = [deel for deel in oordeel.toets.split() if not toets_bestaat(deel)]
        return [f"{oordeel.id}: genoemde toets {deel} bestaat niet" for deel in ontbrekend]
    if oordeel.status is Status.PLAN and not oordeel.plan:
        return [f"{oordeel.id}: status plan zonder een genoemd plan"]
    if oordeel.status is Status.HANDWERK and not oordeel.eigenaar:
        return [f"{oordeel.id}: status handwerk zonder eigenaar"]
    if oordeel.status is Status.NVT and not oordeel.toelichting:
        return [f"{oordeel.id}: status nvt zonder toelichting waarom hij niet van toepassing is"]
    return []


def _dekking(oordeel: Oordeel) -> str:
    """De kolom die zegt waar de dekking zit."""
    if oordeel.status is Status.TOETS:
        return " ".join(f"`{deel}`" for deel in (oordeel.toets or "").split())
    if oordeel.status is Status.PLAN:
        return oordeel.plan or ""
    if oordeel.status is Status.HANDWERK:
        return oordeel.eigenaar or ""
    return oordeel.toelichting or ""


def render_samenvatting(eisen: list[Eis], oordelen: list[Oordeel]) -> str:
    """Per kapittel het aantal eisen per status."""
    per_id = {oordeel.id: oordeel for oordeel in oordelen}
    tellers: dict[str, Counter[Status]] = {}
    for eis in eisen:
        tellers.setdefault(eis.kapittel, Counter())[per_id[eis.id].status] += 1

    regels = [
        "| Kapittel | Eisen | Automatische toets | In een plan | Handwerk | Niet van toepassing |",
        "|---|---|---|---|---|---|",
    ]
    for kapittel, teller in tellers.items():
        regels.append(
            f"| {kapittel} | {sum(teller.values())} | {teller[Status.TOETS]} |"
            f" {teller[Status.PLAN]} | {teller[Status.HANDWERK]} | {teller[Status.NVT]} |"
        )
    totaal = Counter(oordeel.status for oordeel in oordelen)
    regels.append(
        f"| **Totaal** | **{len(eisen)}** | **{totaal[Status.TOETS]}** | **{totaal[Status.PLAN]}** |"
        f" **{totaal[Status.HANDWERK]}** | **{totaal[Status.NVT]}** |"
    )
    return "\n".join(regels)


def render_kapittels(eisen: list[Eis], oordelen: list[Oordeel]) -> str:
    """Per kapittel een tabel met elke eis, zijn status en waar de dekking zit."""
    per_id = {oordeel.id: oordeel for oordeel in oordelen}
    delen = []
    for kapittel in dict.fromkeys(eis.kapittel for eis in eisen):
        delen.append(f"### {kapittel}\n")
        delen.append("| Eis | Niveau | Status | Dekking | Eis uit de standaard |")
        delen.append("|---|---|---|---|---|")
        for eis in (eis for eis in eisen if eis.kapittel == kapittel):
            oordeel = per_id[eis.id]
            delen.append(
                f"| {eis.id} | L{eis.niveau} | {STATUSNAAM[oordeel.status]} | {_dekking(oordeel)} | {eis.eis} |"
            )
        delen.append("")
    return "\n".join(delen)


def render(eisen: list[Eis], niveau: str, oordelen: list[Oordeel]) -> str:
    """Het volledige afvinkdocument, deterministisch uit de twee bronbestanden."""
    return "\n".join(
        [
            "# ASVS-zelfbeoordeling van ZAD",
            "",
            "Gegenereerd uit [architecture/asvs-zelfbeoordeling.yaml](../../architecture/asvs-zelfbeoordeling.yaml)"
            " en [architecture/asvs-5.0-eisen.yaml](../../architecture/asvs-5.0-eisen.yaml) met"
            " `task asvs-afvinklijst`. Bewerk dit bestand niet met de hand: een wijziging hoort in de"
            " beoordeling, en `task asvs-check` meldt het verschil.",
            "",
            f"Meetlat: OWASP ASVS 5.0.0, niveau {niveau} (CC BY-SA 3.0). Per eis staat hier of een"
            " automatische toets hem dekt, welk plan hem bouwt, of dat hij handwerk blijft. Een eis"
            " met status *automatische toets* noemt een toets die bestaat; `task asvs-check` faalt"
            " zodra die verdwijnt.",
            "",
            "De zwaartepunten voor de externe audit zijn autorisatie (V8), tenant-isolatie (V8 en"
            " V13), injectie (V1 en V2), secrets (V11 en V14) en de auth-keten (V6, V7, V9, V10).",
            "",
            "## Dekking per kapittel",
            "",
            render_samenvatting(eisen, oordelen),
            "",
            "## Eisen",
            "",
            render_kapittels(eisen, oordelen),
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render de ASVS-afvinklijst uit de zelfbeoordeling.")
    parser.add_argument("--check", action="store_true", help="exit 1 bij een gat of drift")
    parser.add_argument("--stdout", action="store_true", help="print het document, schrijf het niet")
    args = parser.parse_args(argv)

    eisen = laad_eisen(EISEN)
    niveau, oordelen = laad_beoordeling(BEOORDELING)

    # Eerst de meldingen: zonder een oordeel per eis is er niets te renderen, en dan hoort
    # hier de melding te staan en niet een KeyError uit render().
    gevonden = meldingen(eisen, oordelen)
    if gevonden:
        sys.stderr.write("ASVS-zelfbeoordeling heeft gaten:\n" + "".join(f"  - {m}\n" for m in gevonden))
        return 1

    gerenderd = render(eisen, niveau, oordelen)

    if args.stdout:
        sys.stdout.write(gerenderd)
        return 0

    if args.check:
        huidig = AFVINKLIJST.read_text(encoding="utf-8") if AFVINKLIJST.exists() else ""
        if huidig != gerenderd:
            sys.stderr.write(
                f"{AFVINKLIJST} loopt achter op de beoordeling.\nGenereer opnieuw: task asvs-afvinklijst\n"
            )
            return 1
        sys.stdout.write(f"ASVS-zelfbeoordeling is volledig: {len(eisen)} eisen, elk met een oordeel\n")
        return 0

    AFVINKLIJST.write_text(gerenderd, encoding="utf-8")
    sys.stdout.write(f"schreef {AFVINKLIJST}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
