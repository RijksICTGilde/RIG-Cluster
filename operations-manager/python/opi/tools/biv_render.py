"""Read the BIV inventory, weigh it, and render it as a diagram plus a table.

``architecture/biv-inventaris.yaml`` is the source of truth for the BIV classification
of the ZAD components; ``architecture/biv-overzicht.md`` follows from it and is never
edited by hand. ``tests/test_biv_inventaris.py`` weighs the inventory and reports drift
on the overview.

The BBN is not in the inventory but is derived here: a written-down copy of a derived
value is a second place that can start to differ.

Usage::

    uv run python -m opi.tools.biv_render           # write the overview
    uv run python -m opi.tools.biv_render --check    # exit 1 on drift or on a gap
    uv run python -m opi.tools.biv_render --stdout   # print it, do not write

``--check`` also weighs together with ``--stdout``: a flag that only moves the output
must not switch off the gate.
"""

from __future__ import annotations

import argparse
import sys
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opi.utils.yaml_util import load_yaml_safe

#: The inventory seen from the repo root. Also the marker that identifies that root.
INVENTARIS_FROM_ROOT = Path("architecture") / "biv-inventaris.yaml"


def find_repo_root(start: Path) -> Path:
    """The first directory above ``start`` that holds the inventory.

    A fixed depth only works in a checkout: in the image the code lives at /app/opi/tools,
    where four directories up does not exist. Outside a checkout there is no inventory, and
    then the tool refuses with a message instead of an IndexError.
    """
    for candidate in start.resolve().parents:
        if (candidate / INVENTARIS_FROM_ROOT).is_file():
            return candidate
    raise FileNotFoundError(
        f"geen checkout rond {start}: {INVENTARIS_FROM_ROOT} staat in geen enkele map erboven."
        " De BIV-renderer draait vanuit een checkout van RIG-Cluster, niet in de image."
    )


REPO_ROOT = find_repo_root(Path(__file__))
INVENTARIS = REPO_ROOT / INVENTARIS_FROM_ROOT
OVERZICHT = REPO_ROOT / "architecture" / "biv-overzicht.md"

#: The three axes, in the order BIV names them. Each is also a field of ``Component``.
ASSEN = ("beschikbaarheid", "integriteit", "vertrouwelijkheid")


class Score(StrEnum):
    """A BIV score on one axis."""

    LAAG = "L"
    MIDDEN = "M"
    HOOG = "H"


#: BBN per highest score over the three axes. BBN1 is the base, BBN3 the heaviest regime.
_BBN_PER_SCORE = {Score.LAAG: 1, Score.MIDDEN: 2, Score.HOOG: 3}


class Mitigatie(BaseModel):
    """A measure that covers one axis of a component."""

    model_config = ConfigDict(extra="forbid")

    maatregel: str = Field(min_length=1)
    bron: str = Field(min_length=1)
    toets: str | None = None


class Component(BaseModel):
    """A ZAD component with its BIV score and the mitigations per axis."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^[a-z0-9-]+$")
    naam: str = Field(min_length=1)
    gegevens: str = Field(min_length=1)
    trust_boundary: str = Field(min_length=1, alias="trust-boundary")
    aanvalsproef: bool
    beschikbaarheid: Score
    integriteit: Score
    vertrouwelijkheid: Score
    mitigaties: dict[str, list[Mitigatie]]

    @model_validator(mode="after")
    def _each_axis_has_what_its_score_requires(self) -> Component:
        unknown = set(self.mitigaties) - set(ASSEN)
        if unknown:
            raise ValueError(f"component '{self.id}': onbekende as {sorted(unknown)}")
        for axis in ASSEN:
            if self.score(axis) is not Score.LAAG and not self.mitigaties.get(axis):
                raise ValueError(f"component '{self.id}': {axis} is {self.score(axis)} maar heeft geen mitigatie")
        return self

    def score(self, axis: str) -> Score:
        """The score on one of the three axes."""
        return getattr(self, axis)

    @property
    def highest_score(self) -> Score:
        return max((self.score(a) for a in ASSEN), key=lambda s: _BBN_PER_SCORE[s])

    @property
    def bbn(self) -> int:
        """The BBN that follows from the highest BIV score."""
        return _BBN_PER_SCORE[self.highest_score]


class Inventaris(BaseModel):
    """The complete component inventory."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(alias="schema-version")
    componenten: list[Component] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Inventaris:
        ids = [component.id for component in self.componenten]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"dubbele component-id's: {duplicates}")
        return self

    @property
    def trust_boundaries(self) -> list[str]:
        """The trust boundaries in the order they occur in."""
        return list(dict.fromkeys(component.trust_boundary for component in self.componenten))


def load_inventaris(path: Path = INVENTARIS) -> Inventaris:
    """Read and validate the inventory.

    Raises:
        pydantic.ValidationError: When an entry is incomplete or leaves an axis without a mitigation.
    """
    return Inventaris.model_validate(load_yaml_safe(path.read_text(encoding="utf-8")))


def find_gaps(inventaris: Inventaris, repo_root: Path = REPO_ROOT) -> list[str]:
    """Every axis whose mitigation cannot be proven, as a readable message.

    Three kinds of gap: a score H without any mitigation that names a test, a named test
    that no longer exists, and a named plan or feature document that no longer exists.
    """
    gaps: list[str] = []
    for component in inventaris.componenten:
        for axis in ASSEN:
            mitigaties = component.mitigaties.get(axis, [])
            if component.score(axis) is Score.HOOG and not any(m.toets for m in mitigaties):
                gaps.append(f"{component.id} / {axis}: score H zonder mitigatie met een toets")
            for mitigatie in mitigaties:
                if mitigatie.toets and not (repo_root / mitigatie.toets).exists():
                    gaps.append(f"{component.id} / {axis}: toets '{mitigatie.toets}' bestaat niet")
                if "/" in mitigatie.bron and not (repo_root / mitigatie.bron).exists():
                    gaps.append(f"{component.id} / {axis}: bron '{mitigatie.bron}' bestaat niet")
    return gaps


def aanvalsproef_targets(inventaris: Inventaris) -> list[str]:
    """The component ids the attack proof has to cover.

    The harness of the attack proof reads this list, so no second component list ends up
    next to the inventory.
    """
    return sorted(component.id for component in inventaris.componenten if component.aanvalsproef)


def missing_aanvalsproef_targets(inventaris: Inventaris) -> list[str]:
    """The components with a score H that the attack proof does not name as a target."""
    return sorted(
        component.id
        for component in inventaris.componenten
        if component.highest_score is Score.HOOG and not component.aanvalsproef
    )


def find_problems(inventaris: Inventaris, repo_root: Path = REPO_ROOT) -> list[str]:
    """Everything that makes the check fail: the gaps plus the components outside the proof."""
    return find_gaps(inventaris, repo_root) + [
        f"{component_id}: score H maar geen doel van de aanvalsproef"
        for component_id in missing_aanvalsproef_targets(inventaris)
    ]


def _node(component: Component) -> str:
    scores = "/".join(component.score(a).value for a in ASSEN)
    return f'        {component.id}["{component.naam}<br/>BIV {scores} · BBN{component.bbn}"]'


def render_diagram(inventaris: Inventaris) -> str:
    """A mermaid component diagram: a subgraph per trust boundary, coloured per BBN."""
    rows = ["```mermaid", "graph TB"]
    for boundary in inventaris.trust_boundaries:
        rows.append(f'    subgraph "{boundary}"')
        rows.extend(_node(c) for c in inventaris.componenten if c.trust_boundary == boundary)
        rows.append("    end")
    rows.extend(
        [
            "",
            "    classDef bbn1 fill:#e8f5e9,stroke:#2e7d32",
            "    classDef bbn2 fill:#fff8e1,stroke:#f9a825",
            "    classDef bbn3 fill:#fdecea,stroke:#c62828",
        ]
    )
    for level in (1, 2, 3):
        ids = [c.id for c in inventaris.componenten if c.bbn == level]
        if ids:
            rows.append(f"    class {','.join(ids)} bbn{level}")
    rows.append("```")
    return "\n".join(rows)


def _cell(text: str) -> str:
    """A value as a table cell: a | closes the column and a line break closes the row."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("|", "\\|").replace("\n", "<br/>")


def _mitigation_cell(mitigaties: list[Mitigatie]) -> str:
    """The mitigations of one axis as a cell, with the links relative to architecture/."""
    parts: list[str] = []
    for mitigatie in mitigaties:
        references = [f"[bron](../{mitigatie.bron})"]
        if mitigatie.toets:
            references.append(f"[toets](../{mitigatie.toets})")
        parts.append(f"{_cell(mitigatie.maatregel)} ({', '.join(references)})")
    return "<br/>".join(parts) if parts else "geen"


def render_table(inventaris: Inventaris) -> str:
    """The BIV-plus-mitigation table: one row per component."""
    rows = [
        "| Component | Gegevens | Trust boundary | B | I | V | BBN | Mitigatie beschikbaarheid"
        " | Mitigatie integriteit | Mitigatie vertrouwelijkheid |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for component in inventaris.componenten:
        cells = [
            _cell(component.naam),
            _cell(component.gegevens),
            _cell(component.trust_boundary),
            *(component.score(a).value for a in ASSEN),
            f"BBN{component.bbn}",
            *(_mitigation_cell(component.mitigaties.get(a, [])) for a in ASSEN),
        ]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def render(inventaris: Inventaris) -> str:
    """The complete overview document, deterministic from the inventory."""
    return "\n".join(
        [
            "# BIV-overzicht van de ZAD-componenten",
            "",
            "Gegenereerd uit [architecture/biv-inventaris.yaml](biv-inventaris.yaml) met"
            " `task biv-overzicht`. Bewerk dit bestand niet met de hand: een wijziging hoort in"
            " de inventaris, en `tests/test_biv_inventaris.py` meldt het verschil.",
            "",
            "Het BBN volgt uit de hoogste BIV-score: L geeft BBN1, M geeft BBN2, H geeft BBN3."
            " Dat BBN bepaalt welke BIO2-maatregelen van toepassing zijn en hoe streng.",
            "",
            "## Componenten en trust boundaries",
            "",
            render_diagram(inventaris),
            "",
            "## BIV-score en mitigaties",
            "",
            render_table(inventaris),
            "",
            "## Doelen van de aanvalsproef",
            "",
            "De aanvalsproef leest dezelfde inventaris; deze componenten zijn de doelen:"
            f" {', '.join(aanvalsproef_targets(inventaris))}.",
            "",
        ]
    )


def _check(rendered: str, found: list[str], report_success: bool) -> int:
    """Exit code for --check: 1 on a gap or on drift on the overview.

    ``report_success`` is off with --stdout, so stdout then holds only the overview.
    """
    if found:
        sys.stderr.write("BIV-inventaris heeft gaten:\n" + "".join(f"  - {m}\n" for m in found))
        return 1
    current = OVERZICHT.read_text(encoding="utf-8") if OVERZICHT.exists() else ""
    if current != rendered:
        sys.stderr.write(f"{OVERZICHT} loopt achter op de inventaris.\nGenereer opnieuw: task biv-overzicht\n")
        return 1
    if report_success:
        sys.stdout.write("BIV-overzicht is actueel en zonder gaten\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render het BIV-overzicht uit de inventaris.")
    parser.add_argument("--check", action="store_true", help="exit 1 bij drift of een gat")
    parser.add_argument("--stdout", action="store_true", help="print het overzicht, schrijf het niet")
    args = parser.parse_args(argv)

    inventaris = load_inventaris()
    rendered = render(inventaris)
    found = find_problems(inventaris)

    if args.stdout:
        sys.stdout.write(rendered)

    if args.check:
        return _check(rendered, found, report_success=not args.stdout)

    if not args.stdout:
        OVERZICHT.write_text(rendered, encoding="utf-8")
        sys.stdout.write(f"schreef {OVERZICHT}\n")
        for problem in found:
            sys.stderr.write(f"gat: {problem}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
