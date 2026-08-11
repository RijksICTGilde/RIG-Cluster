"""``c-p`` bestaat niet; het heet ``c-paragraph``.

Het oude componentensysteem kende beide namen (``p`` was een alias van ``paragraph``),
dit systeem alleen de lange. Een achtergebleven ``<c-p>`` levert daardoor geen paragraaf
op maar een fout bij het compileren van dat sjabloon - en die zie je pas als iemand de
pagina opvraagt.

Hier stonden nog twee tests bij: een die bewees dat de twee namen ONDER ROOS identiek
renderden (het bewijs onder de hernoeming), en een die ``c-menubar`` liet staan zolang
roos geen ``menu`` kende. Allebei gingen ze over het oude systeem, en dat is er niet meer.
"""

import pathlib
import re

from opi.core.template_helpers import CATALOG_DIR, TEMPLATES_DIR

# Een componenttag met naam ``p``: ``<c-p>``, ``<c-p ...>``, ``<c-p/>`` en ``</c-p>``.
OUDE_PARAGRAAFTAG = re.compile(r"</?c-p[\s>/]")


def _templatebestanden() -> list[pathlib.Path]:
    """Alle Jinja-templates van het portaal: de eigen map plus die van de diensten."""
    bestanden: list[pathlib.Path] = []
    for map_ in (TEMPLATES_DIR, CATALOG_DIR):
        bestanden.extend(pathlib.Path(map_).rglob("*.html.j2"))
    return bestanden


def test_oude_paragraaftag_komt_nergens_meer_voor() -> None:
    """``c-p`` is overal ``c-paragraph`` geworden, en dat blijft zo."""
    gevonden = [
        f"{pad}:{nr}"
        for pad in _templatebestanden()
        for nr, regel in enumerate(pad.read_text().splitlines(), start=1)
        if OUDE_PARAGRAAFTAG.search(regel)
    ]
    assert gevonden == [], (
        "Deze templates gebruiken nog <c-p>. Die naam bestaat niet; schrijf <c-paragraph>:\n  " + "\n  ".join(gevonden)
    )
