"""Bewaakt het gat tussen onze iconnamen en de NLDD-woordenschat.

Onze templates en dienstdefinities dragen Nederlandse iconnamen uit het oude design
system; NLDD heeft een eigen, Engelse
woordenschat. LOTC vertaalt een handvol namen zelf en geeft de rest ongewijzigd door,
waarna NLDD ze niet herkent en er niets verschijnt. Dat faalt niet - het is stil, en
dat is precies waarom het een test verdient: een leeg icoon ziet niemand in een
foutmelding, alleen op een screenshot.

Elke toets meet de naam die er NA het renderen uitkomt (het ``name=`` op ``<nldd-icon>``)
en niet de naam die wij meegeven: LOTC heeft een eigen aliaslaag in ``icons.json`` die er
nog tussen zit, en daar verdween het icoon van de bijlagendienst in (``folder-stack``
bestaat, wordt herschreven naar ``folder-on-folder``, en die bestaat niet).

De test toetst vier dingen:

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
4. Elke letterlijke iconnaam in de Python-bron levert een icoon op. De dienstDEFINITIES
   waren daarvan maar een deel: een deploymentactie, een formuliersectie en een preset
   dragen hun icoon net zo goed in Python, en daar stonden drie lege plekken.
"""

import logging
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from opi.core.templates_lotc import templates_lotc
from opi.web.menu import get_menu_items
from opi.web.navigation_lotc import ROOS_TO_NLDD_ICONS, to_nldd_icon
from opi.web.nldd_iconen import nldd_icon_names

TEMPLATES_DIR = Path(__file__).parent.parent / "opi" / "templates_lotc"

# Iconen die wij gebruiken en waar NLDD geen tegenhanger voor heeft. Ze renderen leeg.
# Uitgezet bij het LOTC-project; zodra NLDD ze levert horen ze in ROOS_TO_NLDD_ICONS
# en hier weg.
#
# DEZE LIJST IS LEEG, EN DAT HOORT ZO. Een naam hier zetten is zeggen "deze plek blijft
# leeg en dat vinden we goed", en dat is precies de toestand die deze test hoort te
# verhinderen. Staat er iets in, dan hoort erbij te staan waarom er geen enkele
# NLDD-naam de lading dekt - niet dat het even niet uitkwam.
#
# De twee namen die hier stonden, uit-aanknop en weegschaal, hadden inderdaad geen
# letterlijke tegenhanger, maar wel een die hetzelfde zegt: moon voor een slapende
# deployment en score-meter voor de hulppagina over resources. Ze staan nu in
# ROOS_TO_NLDD_ICONS.
KNOWN_GAPS: set[str] = set()


def _gerenderde_naam(icon: str) -> str:
    """De naam die er na LOTC's eigen aliaslaag uitkomt: het ``name=`` op ``<nldd-icon>``.

    DIT IS DE NAAM DIE DE BROWSER OPZOEKT, en hij is niet altijd de naam die wij
    meegeven. ``icons.json`` van lord_of_the_components draagt een tabel met aliassen die
    tijdens het renderen wordt toegepast: ``folder-stack`` -> ``folder-on-folder``,
    ``database`` -> ``cylinder-split``.

    Op die eerste ging het mis. ``folder-stack`` staat WEL in de geleverde bundel, dus
    elke poort hier was groen; ``folder-on-folder`` staat er NIET in, en dat is de naam
    waarmee het icoon van de bijlagendienst werd opgezocht. Het rendeerde leeg. Een poort
    die de naam meet die wij meegeven in plaats van de naam die eruit komt, meet dus de
    verkeerde kant van de aliaslaag.
    """
    html = templates_lotc.env.from_string(f'<c-icon icon="{icon}"/>').render()
    treffer = re.search(r'name="([^"]*)"', html)
    return treffer.group(1) if treffer else icon


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

    HIER STOND DE VERKEERDE BRON, EN DAAR KWAM DE HELE ELLENDE VANDAAN.

    Deze functie las ``icons.json`` van lord_of_the_components: de BEDOELDE woordenschat,
    327 namen. De bundel die de browser laadt bevat er 271. De 56 namen ertussen bestaan
    dus op papier en renderen als niets, en deze test keurde ze goed.

    Gemeten in een browser, met een echte <nldd-icon> en <nldd-button> per naam en de
    vraag of er een pad in het SVG zat: van de 79 iconnamen in de sjablonen renderden er
    37 als een lege plek - waaronder de bewerkknop en de verwijderknop. Deze test was al
    die tijd groen.

    De bron is nu opi/web/nldd_iconen.py: de namen uit de GELEVERDE bestanden, dezelfde
    plek waar de browser ze vandaan haalt.
    """
    namen = nldd_icon_names()
    if not namen:
        pytest.skip("LOTC-thema niet geinstalleerd (dependency-group lotc)")
    return set(namen)


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


#: De Python-kant van de applicatie, waar iconnamen ook letterlijk in de code staan.
OPI_DIR = Path(__file__).parent.parent / "opi"


def _iconen_in_python() -> dict[str, set[str]]:
    """Elke letterlijke ``icon="..."`` in de Python-bron, met de bestanden erbij.

    De dienstDEFINITIES werden al gemeten (hierboven), en dat was maar een deel van de
    Python-kant: een deploymentactie, een formuliersectie en een preset dragen hun icoon
    net zo goed in Python. Daar zaten drie lege plekken die geen enkele poort zag -
    ``raket`` op de wizardstap Deployments, ``uitvoering`` op "Applicatie wekken" en "Job
    uitvoeren", en ``wolk`` op de objectopslagconfiguratie.
    """
    gevonden: dict[str, set[str]] = {}
    for bron in OPI_DIR.rglob("*.py"):
        for naam in re.findall(r'(?<![-\w])icon="([a-z0-9-]+)"', bron.read_text()):
            gevonden.setdefault(naam, set()).add(bron.name)
    return gevonden


def test_de_python_kant_draagt_iconnamen() -> None:
    """Bewaak de bewaker: een lege vangst maakt de test hieronder gratis groen."""
    assert len(_iconen_in_python()) > 10


def test_elke_iconnaam_in_python_levert_een_icoon_op() -> None:
    """Elke letterlijke iconnaam in de code komt na vertaling op een echt icoon uit.

    Deze kant gaat WEL door ROOS_TO_NLDD_ICONS: wat in Python staat komt via het
    ``nldd_icon``-filter of via ``to_nldd_icon()`` de sjabloon in.
    """
    vocabulaire = _nldd_vocabulary()
    leeg = {
        f"{naam} -> {to_nldd_icon(naam)}": sorted(bestanden)
        for naam, bestanden in _iconen_in_python().items()
        if _gerenderde_naam(to_nldd_icon(naam)) not in vocabulaire
    }
    assert not leeg, "iconnamen in Python die als een lege plek renderen:\n" + "\n".join(
        f"  {naam}: {', '.join(bestanden)}" for naam, bestanden in sorted(leeg.items())
    )


def test_every_mapping_points_at_a_real_nldd_icon() -> None:
    """Geen enkele afbeelding wijst naar een naam die NLDD niet kent."""
    vocabulary = _nldd_vocabulary()
    invalid = {
        ours: f"{theirs} -> {_gerenderde_naam(theirs)}"
        for ours, theirs in ROOS_TO_NLDD_ICONS.items()
        if _gerenderde_naam(theirs) not in vocabulary
    }
    assert not invalid, f"afbeelding naar onbekende NLDD-iconen: {invalid}"


def test_elk_dienstpictogram_levert_een_icoon_op() -> None:
    """Elke dienstdefinitie komt NA vertaling in de geleverde woordenschat uit.

    Deze kant mag wel door ROOS_TO_NLDD_ICONS: de dienstdefinities staan in Python en de
    sjablonen halen hun icoon door het ``nldd_icon``-filter. Bij een sjabloon met een
    letterlijke naam gebeurt dat NIET - zie de test daaronder, en dat verschil is precies
    waar 37 lege plekken vandaan kwamen.
    """
    vocabulary = _nldd_vocabulary()
    leeg = {
        icon: to_nldd_icon(icon)
        for icon in _icons_used_by_services()
        if _gerenderde_naam(to_nldd_icon(icon)) not in vocabulary
    }
    assert not leeg, (
        "dienstpictogrammen die als een lege plek renderen (naam -> na vertaling): "
        f"{leeg}. Kies een naam die NLDD levert of leg de afbeelding in ROOS_TO_NLDD_ICONS."
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
    assert _gerenderde_naam(vertaald) in _nldd_vocabulary(), (
        f"menu-icoon {icon!r} wordt {vertaald!r} en dat kent NLDD niet; "
        f"het rendert leeg zonder enige foutmelding. Kies een bestaande naam of leg de "
        f"afbeelding in ROOS_TO_NLDD_ICONS."
    )


def test_unknown_icon_passes_through_unchanged() -> None:
    """Een onbekende naam gaat ongewijzigd door, in plaats van naar een willekeurig icoon."""
    assert to_nldd_icon("bestaat-niet") == "bestaat-niet"
    assert to_nldd_icon("sleutel") == "lock-closed"


def test_een_onbekende_naam_gaat_niet_stil_door(caplog: pytest.LogCaptureFixture) -> None:
    """Doorlaten mag, zwijgen niet.

    Ongewijzigd doorlaten blijft juist - een verkeerd icoon tonen is erger dan een lege
    plek - maar het moet wel ergens langskomen. Zonder deze regel is de uitkomst dat een
    knop ruimte vrijhoudt voor niets en dat niemand er iets over hoort; zo hebben 37 lege
    plekken het maanden volgehouden.
    """
    with caplog.at_level(logging.WARNING, logger="opi.web.navigation_lotc"):
        to_nldd_icon("bestaat-echt-niet")
    assert any("bestaat-echt-niet" in bericht for bericht in caplog.messages), (
        "een iconnaam die geen icoon oplevert ging stil door"
    )


def test_een_naam_die_wel_bestaat_klaagt_niet(caplog: pytest.LogCaptureFixture) -> None:
    """Bewaak de bewaker: een waarschuwing bij elke naam is net zo nutteloos als geen."""
    with caplog.at_level(logging.WARNING, logger="opi.web.navigation_lotc"):
        to_nldd_icon("sleutel")
        to_nldd_icon("file-text")
    assert not caplog.messages, f"onterechte waarschuwing: {caplog.messages}"


# --------------------------------------------------------------- de andere kant op

TEMPLATES_LOTC_DIR = Path(__file__).parent.parent / "opi" / "templates_lotc"

#: De diensten leveren hun eigen sjablonen en die renderen op dezelfde pagina's. Ze
#: stonden hier niet in, en een lege plek in een dienstblok is net zo leeg.
CATALOG_DIR = Path(__file__).parent.parent / "opi" / "services" / "catalog"


def _iconen_in_lotc_templates() -> dict[str, set[str]]:
    """Elke letterlijke ``icon="..."`` in de LOTC-sjablonen, met de bestanden erbij.

    Alleen letterlijke waarden: een ``icon="{{ ... }}"`` komt uit de gegevens en kan hier
    niet beoordeeld worden.
    """
    gevonden: dict[str, set[str]] = {}
    for template in [*TEMPLATES_LOTC_DIR.rglob("*.j2"), *CATALOG_DIR.rglob("*.j2")]:
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

    EN TOCH BLEEF HIJ LEKKEN, op twee manieren tegelijk:

    1. Hij las de bedoelde woordenschat in plaats van de geleverde (zie
       _nldd_vocabulary), dus namen die alleen op papier bestaan kwamen er doorheen.
    2. Hij liet elke naam door die in ROOS_TO_NLDD_ICONS staat. Die uitzondering leek
       logisch en was fout: die tabel wordt toegepast door het ``nldd_icon``-FILTER, en
       een letterlijke ``icon="verwijderen"`` in een sjabloon komt daar nooit langs. De
       naam staat in de tabel, hij wordt niet vertaald, en hij rendeert als niets.
       Die uitzondering is weg; de sjablonen dragen nu NLDD-namen.

    Gemeten in een browser: dit greep 37 van de 79 iconnamen, waaronder de bewerkknop
    naast de projecttitel en de verwijderknop bij een herhaalbaar item.
    """
    vocabulaire = _nldd_vocabulary()
    onbekend = {
        naam: sorted(bestanden)
        for naam, bestanden in _iconen_in_lotc_templates().items()
        if _gerenderde_naam(naam) not in vocabulaire and naam not in KNOWN_GAPS
    }
    assert not onbekend, "iconnamen die NLDD niet kent (ze renderen leeg, zonder foutmelding):\n" + "\n".join(
        f"  {naam}: {', '.join(bestanden)}" for naam, bestanden in sorted(onbekend.items())
    )
