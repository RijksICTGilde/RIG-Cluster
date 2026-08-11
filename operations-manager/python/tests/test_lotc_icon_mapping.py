"""Bewaakt het gat tussen onze iconnamen en de NLDD-woordenschat.

Onze templates en dienstdefinities dragen Nederlandse iconnamen uit het oude design
system; NLDD heeft een eigen, Engelse
woordenschat. LOTC vertaalt een handvol namen zelf en geeft de rest ongewijzigd door,
waarna NLDD ze niet herkent en er niets verschijnt. Dat faalt niet - het is stil, en
dat is precies waarom het een test verdient: een leeg icoon ziet niemand in een
foutmelding, alleen op een screenshot.

De test toetst drie dingen:

1. Elke afbeelding in ROOS_TO_NLDD_ICONS wijst naar een naam die NLDD echt kent.
   Een gok naar een niet-bestaande naam levert hetzelfde lege icoon op als geen
   afbeelding, maar dan onzichtbaar in plaats van meetbaar.
2. De lijst iconen zonder tegenhanger groeit niet. Nieuwe iconen in templates vallen
   daarmee meteen op, in plaats van pas als iemand de pagina bekijkt.
3. Elk icoon in het HOOFDMENU komt na vertaling in die woordenschat uit. Dat stond in
   tests/test_menu_icons_exist.py, dat de SVG-bestanden van jinja-roos-components telde;
   die set is er niet meer, en de vraag is nu welke naam NLDD kent. Het menu apart, want
   het is de enige plek waar de iconnamen uit Python komen en pas na een rollencontrole
   compleet zijn - "Domeinen" droeg lange tijd een naam die niet bestond.
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from opi.web.menu import get_menu_items
from opi.web.navigation_lotc import ROOS_TO_NLDD_ICONS, to_nldd_icon

TEMPLATES_DIR = Path(__file__).parent.parent / "opi" / "templates_lotc"

# Iconen die wij gebruiken en waar NLDD geen tegenhanger voor heeft. Ze renderen leeg.
# Uitgezet bij het LOTC-project; zodra NLDD ze levert horen ze in ROOS_TO_NLDD_ICONS
# en hier weg. Groeit deze lijst, dan is er een icoon bijgekomen zonder dat iemand de
# afbeelding heeft gelegd.
KNOWN_GAPS = {
    # Geen tegenhanger in de NLDD-woordenschat van 271 iconen. De RVO-set die
    # jinja-roos-components meelevert heeft er 1163; of die als losse module in LOTC kan
    # ligt daar als vraag.
    #
    # Deze lijst was lang elf namen langer. Dat kwam niet doordat NLDD ze miste maar
    # doordat deze test de verkeerde lijst las (zie _nldd_vocabulary): trash,
    # question-mark-circle, dismiss en de caret-driehoekjes bestaan gewoon.
    "uit-aanknop",
    "weegschaal",
}


def _nldd_vocabulary() -> set[str]:
    """De iconnamen die NLDD kent, uit de iconenlijst van LOTC zelf.

    Hier stond ``_BUTTON_ICONS_MAP`` uit de NLDD-renderers, en dat was de verkeerde
    lijst: die tabel bevat de zestig iconen die op een KNOP mogen staan, niet de
    woordenschat. De echte set staat in ``icons.json`` van lord_of_the_components en
    telt er 271. Het verschil was niet onschuldig - het maakte van elf iconen die NLDD
    gewoon levert (trash, question-mark-circle, heart) "bekende gaten", waarna ze in
    KNOWN_GAPS belandden en niemand ze meer legde.

    Uit het pakket gelezen en niet overgeschreven: een handgeschreven kopie zou
    stilzwijgend verouderen bij een versiebump, en juist daarvoor is deze test bedoeld.
    """
    lotc = pytest.importorskip(
        "lord_of_the_components", reason="LOTC-bouwlijn niet geinstalleerd (dependency-group lotc)"
    )
    icons = json.loads((Path(lotc.__file__).parent / "icons.json").read_text())
    # De aliassen tellen mee: dat zijn de vriendelijke namen (``database``, ``search``,
    # ``calendar``) die LOTC zelf naar een icoon uit de set vertaalt. Ze renderen dus
    # gewoon, en ze staan al jaren in onze templates.
    return set(icons["sets"]["nldd"]) | set(icons["aliases"])


def _icons_used_in_templates() -> set[str]:
    icons: set[str] = set()
    for template in TEMPLATES_DIR.rglob("*.j2"):
        # De negatieve terugblik houdt ``show-icon="before"`` buiten de vangst: dat is de
        # PLAATS van het icoon en geen naam. Zonder hem meldde deze test "after", "before"
        # en "sort" als iconen zonder afbeelding.
        icons |= set(re.findall(r'(?<![-\w])icon="([a-z0-9-]+)"', template.read_text()))
    return icons


def _icons_used_by_services() -> set[str]:
    """De iconen die de servicedefinities dragen.

    Die staan in Python en niet in een template, en werden daardoor eerst niet
    meegenomen. Juist daar zat het grootste gat: van de negentien zichtbare diensten
    hadden er zeventien geen icoon, en dat viel pas op een screenshot op.
    """
    from opi.services.services import ServiceAdapter

    return {ServiceAdapter.SERVICE_DEFINITIONS[service_type].icon for service_type in ServiceAdapter.get_all_services()}


def _all_icons_in_use() -> set[str]:
    return _icons_used_in_templates() | _icons_used_by_services()


def test_every_mapping_points_at_a_real_nldd_icon() -> None:
    """Geen enkele afbeelding wijst naar een naam die NLDD niet kent."""
    vocabulary = _nldd_vocabulary()
    invalid = {ours: theirs for ours, theirs in ROOS_TO_NLDD_ICONS.items() if theirs not in vocabulary}
    assert not invalid, f"afbeelding naar onbekende NLDD-iconen: {invalid}"


def test_icon_gap_does_not_grow() -> None:
    """De iconen zonder tegenhanger zijn precies de bekende gaten, niet meer."""
    vocabulary = _nldd_vocabulary()
    unmapped = {icon for icon in _all_icons_in_use() if icon not in ROOS_TO_NLDD_ICONS and icon not in vocabulary}
    assert unmapped == KNOWN_GAPS, (
        f"nieuw zonder afbeelding: {sorted(unmapped - KNOWN_GAPS)}; "
        f"niet langer een gat (haal uit KNOWN_GAPS): {sorted(KNOWN_GAPS - unmapped)}"
    )


def _menu_icons() -> set[str]:
    """De iconnamen uit elke variant van het hoofdmenu.

    Beheerder-zijn wordt afgeleid uit het e-mailadres via ``get_user_service()`` en niet
    meegegeven, dus de beheerregels (Domeinen erbij) bereik je alleen door die opzoeking
    te vervangen. Een anonieme render alleen zou juist het item missen waar deze test voor
    bestaat.
    """
    icons: set[str] = set()
    for is_admin in (False, True):
        service = MagicMock()
        service.is_platform_admin.return_value = is_admin
        with patch("opi.web.menu.get_user_service", return_value=service):
            for user in (None, {"email": "someone@example.com"}):
                icons.update(item["icon"] for item in get_menu_items(user=user) if item.get("icon"))
    return icons


def test_the_menu_actually_has_icons() -> None:
    """Bewaak de bewaker: een leeg menu maakt de test hieronder gratis groen."""
    assert len(_menu_icons()) > 3


@pytest.mark.parametrize("icon", sorted(_menu_icons()))
def test_every_menu_icon_lands_in_the_nldd_vocabulary(icon: str) -> None:
    vertaald = to_nldd_icon(icon)
    assert vertaald in _nldd_vocabulary(), (
        f"menu-icoon {icon!r} wordt {vertaald!r} en dat kent NLDD niet; "
        f"het rendert leeg zonder enige foutmelding. Kies een bestaande naam of leg de "
        f"afbeelding in ROOS_TO_NLDD_ICONS."
    )


def test_unknown_icon_passes_through_unchanged() -> None:
    """Een onbekende naam gaat ongewijzigd door, in plaats van naar een willekeurig icoon."""
    assert to_nldd_icon("bestaat-niet") == "bestaat-niet"
    assert to_nldd_icon("sleutel") == "lock-closed"


# --------------------------------------------------------------- de andere kant op

TEMPLATES_LOTC_DIR = Path(__file__).parent.parent / "opi" / "templates_lotc"


def _iconen_in_lotc_templates() -> dict[str, set[str]]:
    """Elke letterlijke ``icon="..."`` in de LOTC-sjablonen, met de bestanden erbij.

    Alleen letterlijke waarden: een ``icon="{{ ... }}"`` komt uit de gegevens en kan hier
    niet beoordeeld worden.
    """
    gevonden: dict[str, set[str]] = {}
    for template in TEMPLATES_LOTC_DIR.rglob("*.j2"):
        # (?<![-\w]) zodat show-icon="before" en start-icon="sort" NIET meetellen: dat zijn
        # andere attributen met een eigen woordenschat.
        for naam in re.findall(r'(?<![-\w])icon="([a-z0-9-]+)"', template.read_text()):
            gevonden.setdefault(naam, set()).add(template.name)
    return gevonden


def test_elke_iconnaam_in_een_lotc_sjabloon_bestaat_in_nldd() -> None:
    """Een naam die NLDD niet kent rendert LEEG, zonder enige foutmelding.

    De test hierboven kijkt de andere kant op: welke ROOS-namen wij gebruiken en of daar
    een afbeelding voor is. Die vangt niet wat er in de LOTC-sjablonen zelf staat, want
    daar schrijven we NLDD-namen rechtstreeks - en een tikfout daarin is onzichtbaar.

    Dit gat kostte vier keer een icoon voordat iemand het op een screenshot zag:
    "stethoscoop" op de servicekaarten, "document" op de logsknop en in het logpaneel,
    "question-circle" op de infoknop van de wizardkaarten (het heet question-mark-circle),
    en de hele set dienstkaarten in de wizard, waar ROOS-namen ongwijzigd doorliepen.
    """
    vocabulaire = _nldd_vocabulary()
    onbekend = {
        naam: sorted(bestanden)
        for naam, bestanden in _iconen_in_lotc_templates().items()
        if naam not in vocabulaire and naam not in ROOS_TO_NLDD_ICONS and naam not in KNOWN_GAPS
    }
    assert not onbekend, "iconnamen die NLDD niet kent (ze renderen leeg, zonder foutmelding):\n" + "\n".join(
        f"  {naam}: {', '.join(bestanden)}" for naam, bestanden in sorted(onbekend.items())
    )
