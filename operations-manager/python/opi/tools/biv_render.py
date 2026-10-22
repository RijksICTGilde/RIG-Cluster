"""De BIV-inventaris lezen, toetsen en als diagram plus tabel renderen.

``architecture/biv-inventaris.yaml`` is de bron van waarheid voor de BIV-classificatie van
de ZAD-componenten; ``architecture/biv-overzicht.md`` volgt eruit en wordt nooit met de
hand bijgewerkt. ``tests/test_biv_inventaris.py`` toetst de inventaris en meldt drift op
het overzicht.

Het BBN staat niet in de inventaris maar wordt hier afgeleid: een opgeschreven kopie van
een afgeleide waarde is een tweede plek die kan gaan afwijken.

Gebruik::

    uv run python -m opi.tools.biv_render           # schrijf het overzicht
    uv run python -m opi.tools.biv_render --check    # exit 1 bij drift of een gat
    uv run python -m opi.tools.biv_render --stdout   # print, schrijf niet

``--check`` toetst ook samen met ``--stdout``: een vlag die alleen de uitvoer verlegt mag de
toets niet uitzetten.
"""

from __future__ import annotations

import argparse
import sys
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

#: De inventaris, gezien vanaf de repo-root. Ook het kenmerk waaraan die root te herkennen is.
INVENTARIS_VANAF_ROOT = Path("architecture") / "biv-inventaris.yaml"


def vind_repo_root(start: Path) -> Path:
    """De eerste map boven ``start`` met de inventaris erin.

    Een vaste diepte werkt alleen in een checkout: in de image staat de code op
    /app/opi/tools, waar vier mappen omhoog niet bestaat. Buiten een checkout is er geen
    inventaris, en dan weigert de tool met een melding in plaats van een IndexError.
    """
    for kandidaat in start.resolve().parents:
        if (kandidaat / INVENTARIS_VANAF_ROOT).is_file():
            return kandidaat
    raise FileNotFoundError(
        f"geen checkout rond {start}: {INVENTARIS_VANAF_ROOT} staat in geen enkele map erboven."
        " De BIV-renderer draait vanuit een checkout van RIG-Cluster, niet in de image."
    )


REPO_ROOT = vind_repo_root(Path(__file__))
INVENTARIS = REPO_ROOT / INVENTARIS_VANAF_ROOT
OVERZICHT = REPO_ROOT / "architecture" / "biv-overzicht.md"

#: De drie assen, in de volgorde waarin ze in BIV worden genoemd.
ASSEN = ("beschikbaarheid", "integriteit", "vertrouwelijkheid")


class Score(StrEnum):
    """Een BIV-score op een as."""

    LAAG = "L"
    MIDDEN = "M"
    HOOG = "H"


#: BBN per hoogste score over de drie assen. BBN1 is de basis, BBN3 het zwaarste regime.
_BBN_PER_SCORE = {Score.LAAG: 1, Score.MIDDEN: 2, Score.HOOG: 3}


class Mitigatie(BaseModel):
    """Een maatregel die een as van een component afdekt."""

    model_config = ConfigDict(extra="forbid")

    maatregel: str = Field(min_length=1)
    bron: str = Field(min_length=1)
    toets: str | None = None


class Component(BaseModel):
    """Een ZAD-component met zijn BIV-score en de mitigaties per as."""

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
    def _elke_as_heeft_wat_zijn_score_vraagt(self) -> Component:
        onbekend = set(self.mitigaties) - set(ASSEN)
        if onbekend:
            raise ValueError(f"component '{self.id}': onbekende as {sorted(onbekend)}")
        for as_naam in ASSEN:
            if self.score(as_naam) is not Score.LAAG and not self.mitigaties.get(as_naam):
                raise ValueError(f"component '{self.id}': {as_naam} is {self.score(as_naam)} maar heeft geen mitigatie")
        return self

    def score(self, as_naam: str) -> Score:
        """De score op een van de drie assen."""
        return getattr(self, as_naam)

    @property
    def hoogste_score(self) -> Score:
        return max((self.score(a) for a in ASSEN), key=lambda s: _BBN_PER_SCORE[s])

    @property
    def bbn(self) -> int:
        """Het BBN dat uit de hoogste BIV-score volgt."""
        return _BBN_PER_SCORE[self.hoogste_score]


class Inventaris(BaseModel):
    """De volledige component-inventaris."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(alias="schema-version")
    componenten: list[Component] = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_zijn_uniek(self) -> Inventaris:
        ids = [component.id for component in self.componenten]
        dubbel = sorted({i for i in ids if ids.count(i) > 1})
        if dubbel:
            raise ValueError(f"dubbele component-id's: {dubbel}")
        return self

    @property
    def trust_boundaries(self) -> list[str]:
        """De trust boundaries in de volgorde waarin ze voorkomen."""
        return list(dict.fromkeys(component.trust_boundary for component in self.componenten))


def load_inventaris(path: Path = INVENTARIS) -> Inventaris:
    """Lees en valideer de inventaris.

    Raises:
        pydantic.ValidationError: Als een entry onvolledig is of een as zonder mitigatie laat.
    """
    return Inventaris.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def gaten(inventaris: Inventaris, repo_root: Path = REPO_ROOT) -> list[str]:
    """Elke as waarvan de mitigatie niet te bewijzen is, als leesbare melding.

    Drie soorten gat: een score H zonder enkele mitigatie met toets, een genoemde toets die
    niet meer bestaat, en een genoemd plan of feature-document dat niet meer bestaat.
    """
    meldingen: list[str] = []
    for component in inventaris.componenten:
        for as_naam in ASSEN:
            mitigaties = component.mitigaties.get(as_naam, [])
            if component.score(as_naam) is Score.HOOG and not any(m.toets for m in mitigaties):
                meldingen.append(f"{component.id} / {as_naam}: score H zonder mitigatie met een toets")
            for mitigatie in mitigaties:
                if mitigatie.toets and not (repo_root / mitigatie.toets).exists():
                    meldingen.append(f"{component.id} / {as_naam}: toets '{mitigatie.toets}' bestaat niet")
                if "/" in mitigatie.bron and not (repo_root / mitigatie.bron).exists():
                    meldingen.append(f"{component.id} / {as_naam}: bron '{mitigatie.bron}' bestaat niet")
    return meldingen


def aanvalsproef_doelen(inventaris: Inventaris) -> list[str]:
    """De component-id's die de aanvalsproef moet dekken.

    Het harnas van de aanvalsproef leest deze lijst, zodat er geen tweede componentlijst
    naast de inventaris komt te staan.
    """
    return sorted(component.id for component in inventaris.componenten if component.aanvalsproef)


def ongedekte_aanvalsproef(inventaris: Inventaris) -> list[str]:
    """De componenten met een score H die de aanvalsproef niet als doel noemt."""
    return sorted(
        component.id
        for component in inventaris.componenten
        if component.hoogste_score is Score.HOOG and not component.aanvalsproef
    )


def meldingen(inventaris: Inventaris, repo_root: Path = REPO_ROOT) -> list[str]:
    """Alles wat de check laat falen: de gaten plus de componenten buiten de aanvalsproef."""
    return gaten(inventaris, repo_root) + [
        f"{component_id}: score H maar geen doel van de aanvalsproef"
        for component_id in ongedekte_aanvalsproef(inventaris)
    ]


def _knoop(component: Component) -> str:
    scores = "/".join(component.score(a).value for a in ASSEN)
    return f'        {component.id}["{component.naam}<br/>BIV {scores} · BBN{component.bbn}"]'


def render_diagram(inventaris: Inventaris) -> str:
    """Een mermaid-componentdiagram: subgraph per trust boundary, inkleuring per BBN."""
    regels = ["```mermaid", "graph TB"]
    for boundary in inventaris.trust_boundaries:
        regels.append(f'    subgraph "{boundary}"')
        regels.extend(_knoop(c) for c in inventaris.componenten if c.trust_boundary == boundary)
        regels.append("    end")
    regels.extend(
        [
            "",
            "    classDef bbn1 fill:#e8f5e9,stroke:#2e7d32",
            "    classDef bbn2 fill:#fff8e1,stroke:#f9a825",
            "    classDef bbn3 fill:#fdecea,stroke:#c62828",
        ]
    )
    for niveau in (1, 2, 3):
        ids = [c.id for c in inventaris.componenten if c.bbn == niveau]
        if ids:
            regels.append(f"    class {','.join(ids)} bbn{niveau}")
    regels.append("```")
    return "\n".join(regels)


def _cel(tekst: str) -> str:
    """Een waarde als tabelcel: een | sluit de kolom en een regelafbreking de rij."""
    genormaliseerd = tekst.replace("\r\n", "\n").replace("\r", "\n")
    return genormaliseerd.replace("|", "\\|").replace("\n", "<br/>")


def _mitigatie_cel(mitigaties: list[Mitigatie]) -> str:
    """De mitigaties van een as als een cel, met de links vanaf architecture/."""
    delen: list[str] = []
    for mitigatie in mitigaties:
        verwijzingen = [f"[bron](../{mitigatie.bron})"]
        if mitigatie.toets:
            verwijzingen.append(f"[toets](../{mitigatie.toets})")
        delen.append(f"{_cel(mitigatie.maatregel)} ({', '.join(verwijzingen)})")
    return "<br/>".join(delen) if delen else "geen"


def render_tabel(inventaris: Inventaris) -> str:
    """De BIV-plus-mitigatie-tabel: een rij per component."""
    regels = [
        "| Component | Gegevens | Trust boundary | B | I | V | BBN | Mitigatie beschikbaarheid"
        " | Mitigatie integriteit | Mitigatie vertrouwelijkheid |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for component in inventaris.componenten:
        cellen = [
            _cel(component.naam),
            _cel(component.gegevens),
            _cel(component.trust_boundary),
            *(component.score(a).value for a in ASSEN),
            f"BBN{component.bbn}",
            *(_mitigatie_cel(component.mitigaties.get(a, [])) for a in ASSEN),
        ]
        regels.append("| " + " | ".join(cellen) + " |")
    return "\n".join(regels)


def render(inventaris: Inventaris) -> str:
    """Het volledige overzichtsdocument, deterministisch uit de inventaris."""
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
            render_tabel(inventaris),
            "",
            "## Doelen van de aanvalsproef",
            "",
            "De aanvalsproef leest dezelfde inventaris; deze componenten zijn de doelen:"
            f" {', '.join(aanvalsproef_doelen(inventaris))}.",
            "",
        ]
    )


def _check(gerenderd: str, gevonden: list[str], meld_succes: bool) -> int:
    """Exitcode voor --check: 1 bij een gat of drift op het overzicht.

    ``meld_succes`` staat uit bij --stdout, zodat stdout dan alleen het overzicht bevat.
    """
    if gevonden:
        sys.stderr.write("BIV-inventaris heeft gaten:\n" + "".join(f"  - {m}\n" for m in gevonden))
        return 1
    huidig = OVERZICHT.read_text(encoding="utf-8") if OVERZICHT.exists() else ""
    if huidig != gerenderd:
        sys.stderr.write(f"{OVERZICHT} loopt achter op de inventaris.\nGenereer opnieuw: task biv-overzicht\n")
        return 1
    if meld_succes:
        sys.stdout.write("BIV-overzicht is actueel en zonder gaten\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render het BIV-overzicht uit de inventaris.")
    parser.add_argument("--check", action="store_true", help="exit 1 bij drift of een gat")
    parser.add_argument("--stdout", action="store_true", help="print het overzicht, schrijf het niet")
    args = parser.parse_args(argv)

    inventaris = load_inventaris()
    gerenderd = render(inventaris)
    gevonden = meldingen(inventaris)

    if args.stdout:
        sys.stdout.write(gerenderd)

    if args.check:
        return _check(gerenderd, gevonden, meld_succes=not args.stdout)

    if not args.stdout:
        OVERZICHT.write_text(gerenderd, encoding="utf-8")
        sys.stdout.write(f"schreef {OVERZICHT}\n")
        for melding in gevonden:
            sys.stderr.write(f"gat: {melding}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
