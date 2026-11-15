"""Read the ASVS self-assessment, weigh it, and render it as a checklist document.

``architecture/asvs-5.0-eisen.yaml`` is the yardstick: the ASVS requirements on L1 and
L2, verbatim from the standard. ``architecture/asvs-zelfbeoordeling.yaml`` is our
assessment per requirement, and ``docs/security/asvs-zelfbeoordeling.md`` follows from
those two and is never edited by hand.

What this gate proves: every requirement on the chosen level has exactly one assessment,
and an assessment that points at an automated test names a test that really exists. A
row pointing at a test file or a workflow job that is not (or no longer) there is a
promise, not coverage.

Usage::

    uv run python -m opi.tools.asvs_render           # write the checklist document
    uv run python -m opi.tools.asvs_render --check    # exit 1 on a gap or on drift
    uv run python -m opi.tools.asvs_render --stdout   # print it, do not write
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from opi.utils.yaml_util import load_yaml_safe

#: Repo root: four directories above this file (opi/tools -> opi -> python -> operations-manager).
REPO_ROOT = Path(__file__).resolve().parents[4]
EISEN = REPO_ROOT / "architecture" / "asvs-5.0-eisen.yaml"
BEOORDELING = REPO_ROOT / "architecture" / "asvs-zelfbeoordeling.yaml"
AFVINKLIJST = REPO_ROOT / "docs" / "security" / "asvs-zelfbeoordeling.md"


class Status(StrEnum):
    """How a requirement is covered."""

    TOETS = "toets"
    PLAN = "plan"
    HANDWERK = "handwerk"
    NVT = "nvt"


#: What each status is called in the document.
STATUS_LABEL = {
    Status.TOETS: "automatische toets",
    Status.PLAN: "in een plan",
    Status.HANDWERK: "handwerk",
    Status.NVT: "niet van toepassing",
}


class Eis(BaseModel):
    """An ASVS requirement, verbatim from the standard."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^V\d+\.\d+\.\d+$")
    niveau: int
    kapittel: str
    sectie: str
    eis: str


class Oordeel(BaseModel):
    """Our assessment of a requirement: where the coverage is, or that it stays manual."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^V\d+\.\d+\.\d+$")
    status: Status
    toets: str | None = None
    plan: str | None = None
    eigenaar: str | None = None
    toelichting: str | None = None


def load_eisen(path: Path = EISEN) -> list[Eis]:
    content = load_yaml_safe(path.read_text(encoding="utf-8"))
    return [Eis(**eis) for eis in content["eisen"]]


def load_beoordeling(path: Path = BEOORDELING) -> tuple[str, list[Oordeel]]:
    """The chosen level and the assessments."""
    content = load_yaml_safe(path.read_text(encoding="utf-8"))
    return str(content["niveau"]), [Oordeel(**row) for row in content["oordelen"]]


def referenced_test_exists(reference: str, root: Path = REPO_ROOT) -> bool:
    """Whether a named test exists.

    A reference is a path (``operations-manager/python/tests/test_csrf_enforcement.py``) or a
    path with a job behind it (``.github/workflows/security.yml#semgrep``). With a job, the
    workflow is also checked for that job.
    """
    path, _, job = reference.partition("#")
    file_path = root / path
    if not file_path.is_file():
        return False
    if not job:
        return True
    content = load_yaml_safe(file_path.read_text(encoding="utf-8")) or {}
    return job in (content.get("jobs") or {})


def find_problems(eisen: list[Eis], oordelen: list[Oordeel]) -> list[str]:
    """What is wrong with the assessment; empty when it is complete and substantiated."""
    found: list[str] = []
    per_id: dict[str, list[Oordeel]] = {}
    for oordeel in oordelen:
        per_id.setdefault(oordeel.id, []).append(oordeel)

    ids = {eis.id for eis in eisen}
    found += [f"{eis_id}: geen oordeel, de eis is niet beoordeeld" for eis_id in sorted(ids - per_id.keys())]
    found += [
        f"{eis_id}: oordeel over een eis die niet op dit niveau in de standaard staat"
        for eis_id in sorted(per_id.keys() - ids)
    ]
    found += [
        f"{eis_id}: {len(rows)} oordelen, dat moet er een zijn"
        for eis_id, rows in sorted(per_id.items())
        if len(rows) > 1
    ]

    for oordeel in oordelen:
        found += _objections(oordeel)
    return found


def _objections(oordeel: Oordeel) -> list[str]:
    """What is wrong with a single assessment."""
    if oordeel.status is Status.TOETS:
        if not oordeel.toets:
            return [f"{oordeel.id}: status toets zonder een genoemde toets"]
        missing = [part for part in oordeel.toets.split() if not referenced_test_exists(part)]
        return [f"{oordeel.id}: genoemde toets {part} bestaat niet" for part in missing]
    if oordeel.status is Status.PLAN and not oordeel.plan:
        return [f"{oordeel.id}: status plan zonder een genoemd plan"]
    if oordeel.status is Status.HANDWERK and not oordeel.eigenaar:
        return [f"{oordeel.id}: status handwerk zonder eigenaar"]
    if oordeel.status is Status.NVT and not oordeel.toelichting:
        return [f"{oordeel.id}: status nvt zonder toelichting waarom hij niet van toepassing is"]
    return []


def _coverage_cell(oordeel: Oordeel) -> str:
    """The column that says where the coverage is."""
    if oordeel.status is Status.TOETS:
        return " ".join(f"`{part}`" for part in (oordeel.toets or "").split())
    if oordeel.status is Status.PLAN:
        return oordeel.plan or ""
    if oordeel.status is Status.HANDWERK:
        return oordeel.eigenaar or ""
    return oordeel.toelichting or ""


def render_samenvatting(eisen: list[Eis], oordelen: list[Oordeel]) -> str:
    """The number of requirements per status, per chapter."""
    per_id = {oordeel.id: oordeel for oordeel in oordelen}
    counters: dict[str, Counter[Status]] = {}
    for eis in eisen:
        counters.setdefault(eis.kapittel, Counter())[per_id[eis.id].status] += 1

    rows = [
        "| Kapittel | Eisen | Automatische toets | In een plan | Handwerk | Niet van toepassing |",
        "|---|---|---|---|---|---|",
    ]
    for kapittel, counter in counters.items():
        rows.append(
            f"| {kapittel} | {sum(counter.values())} | {counter[Status.TOETS]} |"
            f" {counter[Status.PLAN]} | {counter[Status.HANDWERK]} | {counter[Status.NVT]} |"
        )
    totals = Counter(oordeel.status for oordeel in oordelen)
    rows.append(
        f"| **Totaal** | **{len(eisen)}** | **{totals[Status.TOETS]}** | **{totals[Status.PLAN]}** |"
        f" **{totals[Status.HANDWERK]}** | **{totals[Status.NVT]}** |"
    )
    return "\n".join(rows)


def render_kapittels(eisen: list[Eis], oordelen: list[Oordeel]) -> str:
    """A table per chapter with every requirement, its status and where the coverage is."""
    per_id = {oordeel.id: oordeel for oordeel in oordelen}
    parts = []
    for kapittel in dict.fromkeys(eis.kapittel for eis in eisen):
        parts.append(f"### {kapittel}\n")
        parts.append("| Eis | Niveau | Status | Dekking | Eis uit de standaard |")
        parts.append("|---|---|---|---|---|")
        for eis in (eis for eis in eisen if eis.kapittel == kapittel):
            oordeel = per_id[eis.id]
            parts.append(
                f"| {eis.id} | L{eis.niveau} | {STATUS_LABEL[oordeel.status]} | {_coverage_cell(oordeel)} | {eis.eis} |"
            )
        parts.append("")
    return "\n".join(parts)


def render(eisen: list[Eis], niveau: str, oordelen: list[Oordeel]) -> str:
    """The complete checklist document, deterministic from the two source files."""
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

    eisen = load_eisen(EISEN)
    niveau, oordelen = load_beoordeling(BEOORDELING)

    # The problems first: without an assessment per requirement there is nothing to render,
    # and the report belongs here instead of as a KeyError out of render().
    found = find_problems(eisen, oordelen)
    if found:
        sys.stderr.write("ASVS-zelfbeoordeling heeft gaten:\n" + "".join(f"  - {m}\n" for m in found))
        return 1

    rendered = render(eisen, niveau, oordelen)

    if args.stdout:
        sys.stdout.write(rendered)
        return 0

    if args.check:
        current = AFVINKLIJST.read_text(encoding="utf-8") if AFVINKLIJST.exists() else ""
        if current != rendered:
            sys.stderr.write(
                f"{AFVINKLIJST} loopt achter op de beoordeling.\nGenereer opnieuw: task asvs-afvinklijst\n"
            )
            return 1
        sys.stdout.write(f"ASVS-zelfbeoordeling is volledig: {len(eisen)} eisen, elk met een oordeel\n")
        return 0

    AFVINKLIJST.write_text(rendered, encoding="utf-8")
    sys.stdout.write(f"schreef {AFVINKLIJST}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
