"""Bewaakt het gat tussen onze iconnamen en de NLDD-woordenschat.

Onze templates dragen Nederlandse ROOS-iconnamen; NLDD heeft een eigen, Engelse
woordenschat. LOTC vertaalt een handvol namen zelf en geeft de rest ongewijzigd door,
waarna NLDD ze niet herkent en er niets verschijnt. Dat faalt niet - het is stil, en
dat is precies waarom het een test verdient: een leeg icoon ziet niemand in een
foutmelding, alleen op een screenshot.

De test toetst twee dingen:

1. Elke afbeelding in ROOS_TO_NLDD_ICONS wijst naar een naam die NLDD echt kent.
   Een gok naar een niet-bestaande naam levert hetzelfde lege icoon op als geen
   afbeelding, maar dan onzichtbaar in plaats van meetbaar.
2. De lijst iconen zonder tegenhanger groeit niet. Nieuwe ROOS-iconen in templates
   vallen daarmee meteen op, in plaats van pas als iemand de pagina bekijkt.
"""

import re
from pathlib import Path

import pytest
from opi.web.navigation_lotc import ROOS_TO_NLDD_ICONS, to_nldd_icon

TEMPLATES_DIR = Path(__file__).parent.parent / "opi" / "templates"

# Iconen die wij gebruiken en waar NLDD geen tegenhanger voor heeft. Ze renderen leeg.
# Uitgezet bij het LOTC-project; zodra NLDD ze levert horen ze in ROOS_TO_NLDD_ICONS
# en hier weg. Groeit deze lijst, dan is er een icoon bijgekomen zonder dat iemand de
# afbeelding heeft gelegd.
KNOWN_GAPS = {
    "delta-naar-links",
    "delta-naar-rechts",
    "delta-omlaag",
    "downloaden",
    "kruis",
    "refresh",
    "terug",
    "uit-aanknop",
    "verwijderen",
    "vraagteken",
    "weegschaal",
}


def _nldd_vocabulary() -> set[str]:
    """De iconnamen die de NLDD-implementatie kent, uit het pakket zelf.

    Uit de gegenereerde renderers gelezen in plaats van overgeschreven: een
    handgeschreven kopie zou stilzwijgend verouderen bij een versiebump, en juist
    daarvoor is deze test bedoeld.
    """
    lotc_nldd = pytest.importorskip("lotc_nldd", reason="LOTC-bouwlijn niet geinstalleerd (dependency-group lotc)")
    source = (Path(lotc_nldd.__file__).parent / "renderers.py").read_text()
    match = re.search(r"_BUTTON_ICONS_MAP = \{(.*?)\}", source, re.DOTALL)
    assert match, "kon de iconentabel niet vinden in de NLDD-renderers"
    keys = set(re.findall(r"'([^']+)':", match.group(1)))
    values = set(re.findall(r":\s*'([^']+)'", match.group(1)))
    return keys | values


def _icons_used_in_templates() -> set[str]:
    icons: set[str] = set()
    for template in TEMPLATES_DIR.rglob("*.j2"):
        icons |= set(re.findall(r'icon="([a-z0-9-]+)"', template.read_text()))
    return icons


def test_every_mapping_points_at_a_real_nldd_icon() -> None:
    """Geen enkele afbeelding wijst naar een naam die NLDD niet kent."""
    vocabulary = _nldd_vocabulary()
    invalid = {ours: theirs for ours, theirs in ROOS_TO_NLDD_ICONS.items() if theirs not in vocabulary}
    assert not invalid, f"afbeelding naar onbekende NLDD-iconen: {invalid}"


def test_icon_gap_does_not_grow() -> None:
    """De iconen zonder tegenhanger zijn precies de bekende gaten, niet meer."""
    vocabulary = _nldd_vocabulary()
    unmapped = {
        icon for icon in _icons_used_in_templates() if icon not in ROOS_TO_NLDD_ICONS and icon not in vocabulary
    }
    assert unmapped == KNOWN_GAPS, (
        f"nieuw zonder afbeelding: {sorted(unmapped - KNOWN_GAPS)}; "
        f"niet langer een gat (haal uit KNOWN_GAPS): {sorted(KNOWN_GAPS - unmapped)}"
    )


def test_unknown_icon_passes_through_unchanged() -> None:
    """Een onbekende naam gaat ongewijzigd door, in plaats van naar een willekeurig icoon."""
    assert to_nldd_icon("bestaat-niet") == "bestaat-niet"
    assert to_nldd_icon("sleutel") == "lock-closed"
