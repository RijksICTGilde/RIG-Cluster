"""Bewaakt de schrijfwijze van de teksten in de LOTC-bouwlijn.

Twee afspraken, allebei makkelijk te vergeten zodra iemand snel een zin toevoegt:

1. **Informeel Nederlands.** We spreken de lezer aan met "je", niet met "u". Dat is een
   keuze over hoe het platform klinkt, en hij is alleen geloofwaardig als hij overal
   geldt: een enkele "uw project" tussen de rest valt harder op dan wanneer je consequent
   formeel zou zijn.

2. **Nederlands, geen half-Engels.** Vaktermen die iedereen zo gebruikt (deployment,
   cluster, namespace) blijven; het gaat om woorden waar gewoon een Nederlands woord voor
   is.

Deze test keek eerst alleen naar de hertekende pagina's. Dat bleek te smal: de wizard
toonde "Basisinformatie over uw project", en die zin staat niet in een template maar in de
FORMULIERDEFINITIES (opi/forms/) - het zijn Python-strings die pas op het scherm komen.
Achttien van zulke zinnen stonden er, inclusief foutmeldingen als "Uw rol: ...". Een test
die de helft van de teksten niet ziet, geeft een vals gevoel van dekking, dus kijkt hij nu
ook daar.

De gegenereerde templates blijven erbuiten: die nemen hun tekst over uit ``opi/templates/``
en volgen de bewoording van de applicatie zelf. Verander je die, dan is dat een besluit
over de productietekst.
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

# De teksten die de wizard en de foutmeldingen tonen. Die staan als Python-string in de
# code en niet in een template, en glipten daardoor langs de eerste versie van deze test.
TEXT_IN_CODE = [
    *sorted((PYTHON_DIR / "opi" / "forms").rglob("*.py")),
    PYTHON_DIR / "opi" / "web" / "router.py",
    PYTHON_DIR / "opi" / "api" / "invite_routes.py",
    PYTHON_DIR / "opi" / "services" / "project_store.py",
]

# "u" als los woord, en de bezittelijke vorm. Woordgrenzen zijn nodig: zonder zou elke
# "u" in "uur" of "nu" meetellen.
FORMAL = re.compile(r"\b(?:U|Uw|uw)\b")


def test_texts_address_the_reader_informally() -> None:
    """Nergens "u" of "uw"; we schrijven "je"."""
    offenders: dict[str, list[str]] = {}
    for path in [*OWN_FILES, *TEXT_IN_CODE]:
        if not path.exists():
            continue
        hits = [line.strip() for line in path.read_text().splitlines() if FORMAL.search(line)]
        if hits:
            offenders[path.name] = hits
    assert not offenders, f"formele aanspreekvorm gevonden; schrijf 'je' in plaats van 'u'/'uw': {offenders}"
