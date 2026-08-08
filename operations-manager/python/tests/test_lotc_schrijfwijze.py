"""Bewaakt de schrijfwijze van de teksten in de LOTC-bouwlijn.

Twee afspraken, allebei makkelijk te vergeten zodra iemand snel een zin toevoegt:

1. **Informeel Nederlands.** We spreken de lezer aan met "je", niet met "u". Dat is een
   keuze over hoe het platform klinkt, en hij is alleen geloofwaardig als hij overal
   geldt: een enkele "uw project" tussen de rest valt harder op dan wanneer je consequent
   formeel zou zijn.

2. **Nederlands, geen half-Engels.** Vaktermen die iedereen zo gebruikt (deployment,
   cluster, namespace) blijven; het gaat om woorden waar gewoon een Nederlands woord voor
   is.

Deze test kijkt alleen naar wat WIJ schrijven: de hertekende pagina's, de schil en de
voorbeelddata. De gegenereerde templates nemen hun tekst over uit ``opi/templates/`` en
volgen dus de bewoording van de applicatie zelf; die veranderen hoort bij een besluit
over de productietekst, niet bij deze bouwlijn.
"""

import re
from pathlib import Path

PYTHON_DIR = Path(__file__).parent.parent
BG_DIR = PYTHON_DIR / "opi" / "templates_lotc" / "bg"

# Wat wij zelf schrijven. Met de hand opgesomd en niet met een glob: de gegenereerde
# templates staan in dezelfde map en die vallen hier bewust buiten.
OWN_FILES = [
    *sorted(BG_DIR.glob("*.j2")),
    PYTHON_DIR / "opi" / "templates_lotc" / "base_lotc.html.j2",
    PYTHON_DIR / "opi" / "templates_lotc" / "form-preview.html.j2",
    PYTHON_DIR / "opi" / "web" / "lotc_form_preview.py",
    PYTHON_DIR / "opi" / "web" / "lotc_fixtures.py",
]

# "u" als los woord, en de bezittelijke vorm. Woordgrenzen zijn nodig: zonder zou elke
# "u" in "uur" of "nu" meetellen.
FORMAL = re.compile(r"\b(?:U|Uw|uw)\b")


def test_texts_address_the_reader_informally() -> None:
    """Nergens "u" of "uw"; we schrijven "je"."""
    offenders: dict[str, list[str]] = {}
    for path in OWN_FILES:
        if not path.exists():
            continue
        hits = [line.strip() for line in path.read_text().splitlines() if FORMAL.search(line)]
        if hits:
            offenders[path.name] = hits
    assert not offenders, f"formele aanspreekvorm gevonden; schrijf 'je' in plaats van 'u'/'uw': {offenders}"
