"""Welke iconen het NLDD-thema echt LEVERT.

Waarom dit bestand bestaat, en waarom het niet gewoon ``icons.json`` leest: die lijst is
de bedoelde woordenschat en telt 327 namen, terwijl de bundel die de browser laadt er 271
bevat. Die 56 namen ertussen bestaan dus op papier en renderen als niets - ``media-pause``
en ``square-arrow-down`` zijn er twee van, en die stonden allebei in het logpaneel.

Dat verschil is niet theoretisch. De iconentoets las jarenlang ``icons.json``, was groen,
en ondertussen stonden er lege plekken in de interface. Een poort die de verkeerde bron
leest is erger dan geen poort: hij geeft je het gevoel dat het gedekt is.

De namen worden daarom uit de GELEVERDE bestanden gehaald - de plek waar de browser ze
ook vandaan haalt - en niet met de hand overgeschreven. Een handgeschreven kopie
veroudert stilzwijgend bij een versiebump, en juist daartegen is dit bedoeld.

Twee soorten namen tellen mee:

- de iconen zelf, die in de bundel als ``["naam", "<svg ...>"]`` staan;
- de vriendelijke namen die NLDD zelf doorverwijst (``search`` -> ``magnifier``,
  ``delete`` -> ``trash``, ``info`` -> ``info-circle``). Die renderen gewoon, dus ze
  horen bij de woordenschat.

Gemeten tegen een browser: 79 namen uit de sjablonen door een echte ``<nldd-icon>`` en
``<nldd-button>`` gehaald en gekeken of er een pad in het SVG zat. De set die daar
uitkwam is precies de set die dit bestand oplevert.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

#: Een icoon in de bundel: ["naam", "<svg ...".
_ICOON = re.compile(r'\["([a-z0-9][a-z0-9-]{1,50})",[\'"]<svg')

#: Een doorverwijzing in de bundel: naam:"bestaand-icoon" of "naam":"bestaand-icoon".
_ALIAS = re.compile(r'(?:"([a-z0-9-]+)"|([a-z][a-z0-9]*)):"([a-z0-9-]+)"')


def _dist_map() -> Path | None:
    """De dist-map van lotc_nldd, waar hij ook geinstalleerd is."""
    try:
        import lotc_nldd
    except ImportError:
        return None

    if lotc_nldd.__file__ is None:
        return None

    map_ = Path(lotc_nldd.__file__).parent / "static" / "lotc" / "nldd" / "dist"
    return map_ if map_.is_dir() else None


@cache
def nldd_icon_names() -> frozenset[str]:
    """Elke iconnaam die de geleverde NLDD-bundel echt tekent.

    Leeg als het thema niet geinstalleerd is. Dat is met opzet: de aanroepers gebruiken
    deze set om te WAARSCHUWEN, en een omgeving zonder thema hoort niet bij elk icoon te
    gaan klagen. De test die hem als poort gebruikt slaat zichzelf dan over.
    """
    map_ = _dist_map()
    if map_ is None:
        return frozenset()

    iconen: set[str] = set()
    bronnen: list[str] = []
    for bestand in sorted(map_.glob("*.js")):
        inhoud = bestand.read_text(encoding="utf-8", errors="ignore")
        bronnen.append(inhoud)
        iconen |= {m.group(1) for m in _ICOON.finditer(inhoud)}

    aliassen: set[str] = set()
    for inhoud in bronnen:
        for m in _ALIAS.finditer(inhoud):
            naam = m.group(1) or m.group(2)
            if m.group(3) in iconen:
                aliassen.add(naam)

    return frozenset(iconen | aliassen)
