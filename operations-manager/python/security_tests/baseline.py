"""De ingecheckte lijst met bekende, geaccepteerde bevindingen.

Een bevinding die hier staat laat de proef niet falen; een bevinding die er niet staat wel.
Andersom ook: staat er een regel die niet meer voorkomt, dan is de bevinding opgelost en moet
de regel weg. Zo blijft de lijst de plek waar een reviewer ziet wat we nog accepteren.

De kolom ``doel`` begrenst een regel tot een soort doel. Leeg is elk doel; ``lokaal`` dekt wat
alleen in de wegwerp-sandbox geldt (geen database, geen cluster), zodat zo'n reden tegen een
sandbox in het cluster geen bevinding wegneemt en daar ook geen OPGELOST oplevert.
"""

from __future__ import annotations

import csv
from typing import TYPE_CHECKING, NamedTuple

from security_tests.doel import PROEF_ROOT

if TYPE_CHECKING:
    from pathlib import Path

#: Tab-gescheiden, met de kolommen proef, doel, kenmerk, reden en bron.
BASELINE = PROEF_ROOT / "bekende-bevindingen.tsv"


class BaselineOordeel(NamedTuple):
    """Wat de baseline van een reeks bevindingen vindt: wat nieuw is, en wat niets meer dekt."""

    nieuw: dict[str, str]
    verouderd: dict[str, str]


def lees_baseline(proef: str, doelsoort: str = "", pad: Path = BASELINE) -> dict[str, str]:
    """De geaccepteerde bevindingen van een proef op dit doel, als kenmerk naar reden.

    Een lege ``doelsoort`` leest de hele lijst; een doel leest de regels zonder doel plus de
    regels van dat doel.
    """
    if not pad.exists():
        return {}
    with pad.open(newline="") as bestand:
        rijen = csv.DictReader(bestand, delimiter="\t")
        return {
            rij["kenmerk"]: rij["reden"]
            for rij in rijen
            if rij["proef"] == proef and (not doelsoort or not rij["doel"] or rij["doel"] == doelsoort)
        }


def oordeel_tegen_baseline(
    proef: str, bevindingen: dict[str, str], doelsoort: str = "", pad: Path = BASELINE
) -> BaselineOordeel:
    """De bevindingen zonder uitleg, en de uitleg die niets meer dekt."""
    baseline = lees_baseline(proef, doelsoort, pad)
    return BaselineOordeel(
        nieuw={kenmerk: melding for kenmerk, melding in bevindingen.items() if kenmerk not in baseline},
        verouderd={kenmerk: reden for kenmerk, reden in baseline.items() if kenmerk not in bevindingen},
    )


def meldingen_van(oordeel: BaselineOordeel) -> list[str]:
    """Het oordeel als meldingen voor een lezer: eerst wat nieuw is, dan wat niets meer dekt."""
    nieuw = [f"NIEUW {kenmerk}: {melding}" for kenmerk, melding in sorted(oordeel.nieuw.items())]
    verouderd = [
        f"OPGELOST {kenmerk}: staat in {BASELINE.name} maar komt niet meer voor, haal de regel weg"
        for kenmerk in sorted(oordeel.verouderd)
    ]
    return nieuw + verouderd


def toets_tegen_baseline(
    proef: str, bevindingen: dict[str, str], doelsoort: str = "", pad: Path = BASELINE
) -> list[str]:
    """De meldingen die overblijven: nieuwe bevindingen, en baselineregels die niets meer dekken."""
    return meldingen_van(oordeel_tegen_baseline(proef, bevindingen, doelsoort, pad))
