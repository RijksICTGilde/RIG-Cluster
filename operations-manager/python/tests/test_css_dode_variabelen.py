"""Een CSS-variabele die niet bestaat verdwijnt STIL. Deze poort maakt hem luid.

Onze stylesheets zijn geschreven tegen de variabelen van het oude thema. Dat thema wordt
niet meer geladen; wat ervan nodig is staat als shim onderaan ``static/css/lotc-app.css``.
Verwijst een regel naar een naam die daar niet staat, dan is die declaratie ongeldig en
doet de browser er niets mee: een gap wordt geen ruimte, een achtergrond geen kleur, een
rand geen rand. De HTML klopt, de klasse staat er, en het scherm is stuk - precies het
soort fout dat geen enkele gedragsmeting oppikt.

Dezelfde eis als bij de iconen, en om dezelfde reden: meet tegen wat er ECHT geleverd
wordt, niet tegen een lijst die ernaast ligt.

DE MEETUITKOMST, zodat de volgende hem niet opnieuw hoeft te doen. Bij het schrijven:

    383 verwijzingen naar var(--rvo-*), 56 unieke namen, 31 gedefinieerd in de shim.
    25 namen bleven onopgelost. Daarvan bestond er EEN in het tokenbestand van het oude
    thema (``--rvo-space-2xs`` = 4px); die was dus echt kapotgegaan en staat nu in de
    shim. De andere 24 bestonden ook in het oude thema niet - schrijffouten en verzonnen
    namen (``grijs-25`` naast ``grijs-050``, ``hemelblauw-50`` naast ``hemelblauw-150``) -
    en deden daar dus ook al niets.

Die 24 staan hieronder als AFTELLIJST en niet als ontheffing. Ze alsnog invullen is een
ontwerpbesluit (welke kleur wordt het dan?) en dat hoort bij het blok waar ze staan,
samen met de vraag of daar niet gewoon een component voor is. Elke keer dat zo'n blok
vervangen wordt, kan hier een regel weg.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

CSS_DIR = Path(__file__).resolve().parent.parent / "static" / "css"

#: Het tokenbestand van het oude thema. Nog geinstalleerd (lotc-rvo staat als pakket in
#: de omgeving, maar niet in DESIGN_SYSTEMS), en daarmee de enige bron die kan zeggen of
#: een naam ooit echt bestond.
RVO_TOKENS = (
    Path(__file__).resolve().parent.parent
    / ".venv/lib/python3.14/site-packages/lotc_rvo/static/lotc/dist/@nl-rvo/design-tokens/index.css"
)

GEBRUIK = re.compile(r"var\(\s*(--rvo-[a-z0-9-]+)")
DEFINITIE = re.compile(r"(--rvo-[a-z0-9-]+)\s*:")

#: Namen die in ONS CSS gebruikt worden en nergens bestaan - ook niet in het oude thema.
#: Per naam het aantal verwijzingen. Het getal mag alleen omlaag.
NOOIT_BESTAAN = {
    "--rvo-border-radius-lg": 1,
    "--rvo-color-error": 1,
    "--rvo-color-geel-150": 1,
    "--rvo-color-geel-300": 3,
    "--rvo-color-geel-750": 2,
    "--rvo-color-grasgroen-200": 1,
    "--rvo-color-grasgroen-800": 1,
    "--rvo-color-grijs-025": 1,
    "--rvo-color-grijs-25": 5,
    "--rvo-color-grijs-50": 4,
    "--rvo-color-groen-50": 3,
    "--rvo-color-hemelblauw-100": 4,
    "--rvo-color-hemelblauw-200": 2,
    "--rvo-color-hemelblauw-50": 4,
    "--rvo-color-hemelblauw-700": 1,
    "--rvo-color-oranje-200": 1,
    "--rvo-color-oranje-50": 2,
    "--rvo-color-oranje-800": 1,
    "--rvo-color-paars": 2,
    "--rvo-color-paars-50": 1,
    "--rvo-color-rood-100": 1,
    "--rvo-color-rood-50": 2,
    "--rvo-color-rood-700": 1,
    "--rvo-color-rood-800": 1,
}


def _gebruikt() -> Counter[str]:
    tellen: Counter[str] = Counter()
    for pad in sorted(CSS_DIR.glob("*.css")):
        tellen.update(GEBRUIK.findall(pad.read_text()))
    return tellen


def _gedefinieerd() -> set[str]:
    namen: set[str] = set()
    for pad in sorted(CSS_DIR.glob("*.css")):
        namen |= set(DEFINITIE.findall(pad.read_text()))
    return namen


def _waar(naam: str) -> list[str]:
    return [pad.name for pad in sorted(CSS_DIR.glob("*.css")) if naam in pad.read_text()]


def test_geen_nieuwe_dode_variabele() -> None:
    """Elke var(--rvo-*) lost op, op de vastgelegde aftellijst na."""
    gebruikt = _gebruikt()
    gedefinieerd = _gedefinieerd()
    dood = {naam: aantal for naam, aantal in gebruikt.items() if naam not in gedefinieerd}

    nieuw = {naam: (aantal, _waar(naam)) for naam, aantal in dood.items() if naam not in NOOIT_BESTAAN}
    assert not nieuw, f"nieuwe dode CSS-variabele - de declaratie eromheen doet niets: {nieuw}"

    gegroeid = {
        naam: (NOOIT_BESTAAN[naam], aantal)
        for naam, aantal in dood.items()
        if naam in NOOIT_BESTAAN and aantal > NOOIT_BESTAAN[naam]
    }
    assert not gegroeid, f"meer verwijzingen naar een dode variabele dan vastgelegd (was, is): {gegroeid}"


def test_de_aftellijst_telt_af() -> None:
    """Een naam die opgeruimd is hoort uit de lijst; anders vergrendelt hij een fout."""
    gebruikt = _gebruikt()
    gedefinieerd = _gedefinieerd()
    overbodig = {
        naam: (aantal, gebruikt.get(naam, 0))
        for naam, aantal in NOOIT_BESTAAN.items()
        if naam in gedefinieerd or gebruikt.get(naam, 0) < aantal
    }
    assert not overbodig, f"minder verwijzingen dan vastgelegd - werk de lijst bij (was, is): {overbodig}"


def test_de_aftellijst_bevat_alleen_namen_die_nooit_bestonden() -> None:
    """De grens: wat het oude thema WEL had is kapotgegaan en hoort in de shim.

    ``--rvo-space-2xs`` was zo'n geval: hij stond in het tokenbestand van het oude thema
    (4px), niet in onze shim, en de ``calc()`` in ``--rvo-alert-gap`` die hem gebruikte
    was daardoor in zijn geheel ongeldig.
    """
    if not RVO_TOKENS.exists():
        return
    van_het_thema = set(DEFINITIE.findall(RVO_TOKENS.read_text()))
    ten_onrechte = sorted(naam for naam in NOOIT_BESTAAN if naam in van_het_thema)
    assert not ten_onrechte, (
        f"deze namen bestonden WEL in het oude thema en zijn dus kapotgegaan; "
        f"zet ze met hun eigen waarde in de shim in lotc-app.css: {ten_onrechte}"
    )


def test_de_shim_verwijst_niet_naar_iets_wat_hij_zelf_niet_heeft() -> None:
    """Een calc() met een onbekende variabele erin levert NIETS op, niet een deel."""
    shim = (CSS_DIR / "lotc-app.css").read_text()
    gedefinieerd = set(DEFINITIE.findall(shim))
    ontbrekend = {naam for naam in GEBRUIK.findall(shim) if naam not in gedefinieerd}
    assert not ontbrekend, f"de shim gebruikt een variabele die hij niet definieert: {sorted(ontbrekend)}"
