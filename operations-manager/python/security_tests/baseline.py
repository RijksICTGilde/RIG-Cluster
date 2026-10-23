"""De ingecheckte lijst met bekende, geaccepteerde bevindingen.

Een bevinding die hier staat laat de proef niet falen; een bevinding die er niet staat wel.
Andersom ook: staat er een regel die niet meer voorkomt, dan is de bevinding opgelost en moet
de regel weg. Zo blijft de lijst de plek waar een reviewer ziet wat we nog accepteren.
"""

from __future__ import annotations

import csv
from typing import TYPE_CHECKING

from security_tests.doel import PROEF_ROOT

if TYPE_CHECKING:
    from pathlib import Path

#: Tab-gescheiden, met de kolommen proef, kenmerk, reden en bron.
BASELINE = PROEF_ROOT / "bekende-bevindingen.tsv"


def lees_baseline(proef: str, pad: Path = BASELINE) -> dict[str, str]:
    """De geaccepteerde bevindingen van een proef, als kenmerk naar reden."""
    if not pad.exists():
        return {}
    with pad.open(newline="") as bestand:
        rijen = csv.DictReader(bestand, delimiter="\t")
        return {rij["kenmerk"]: rij["reden"] for rij in rijen if rij["proef"] == proef}


def toets_tegen_baseline(proef: str, bevindingen: dict[str, str]) -> list[str]:
    """De meldingen die overblijven: nieuwe bevindingen, en baselineregels die niets meer dekken."""
    baseline = lees_baseline(proef)
    nieuw = [
        f"NIEUW {kenmerk}: {melding}" for kenmerk, melding in sorted(bevindingen.items()) if kenmerk not in baseline
    ]
    verouderd = [
        f"OPGELOST {kenmerk}: staat in {BASELINE.name} maar komt niet meer voor, haal de regel weg"
        for kenmerk in sorted(baseline)
        if kenmerk not in bevindingen
    ]
    return nieuw + verouderd
