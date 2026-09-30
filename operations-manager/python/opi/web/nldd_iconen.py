"""Welke iconen het NLDD-thema echt LEVERT.

Waarom dit bestand bestaat, en waarom het niet gewoon ``icons.json`` leest: die lijst is
de bedoelde woordenschat, en de bundel die de browser laadt is wat er werkelijk getekend
wordt. Die twee lopen uiteen, en een naam die alleen op papier bestaat rendert als niets,
zonder foutmelding.

Hoe groot dat gat is verschilt per versie, dus het getal hieronder is een meting en geen
eigenschap. Op NLDD 0.8.92 (pin 172300a) telt ``icons.json`` onder de set ``nldd`` 677
namen en levert dit bestand er 686; het gat is 2 namen die wel in de lijst staan en niets
tekenen: ``stack-code`` en ``rectangle-stack-chevron-left-forward-slash-chevron-right``.

En het loopt per versie de andere kant op. Op 0.8.80 waren ``media-pause`` en
``square-arrow-down`` juist de namen die leeg renderden en zat ``square-and-arrow-down``
wel in de bundel; op 0.8.92 is dat omgedraaid en tekenen die eerste twee gewoon. Een naam
kiezen omdat hij ooit gemeten is, is dus niet genoeg: meet hem opnieuw bij een bump, want
het blijft een leeg ``<svg>`` zonder een enkele tekening erin.

Dat verschil is niet theoretisch. De iconentoets las jarenlang ``icons.json``, was groen,
en ondertussen stonden er lege plekken in de interface. Een poort die de verkeerde bron
leest is erger dan geen poort: hij geeft je het gevoel dat het gedekt is.

De namen worden daarom uit de GELEVERDE bestanden gehaald - de plek waar de browser ze
ook vandaan haalt - en niet met de hand overgeschreven. Een handgeschreven kopie
veroudert stilzwijgend bij een versiebump, en juist daartegen is dit bedoeld.

Twee soorten namen tellen mee:

- de iconen zelf, die in de bundel als ``["naam", "<svg ...>"]`` staan: 358 op 0.8.92;
- de vriendelijke namen die NLDD zelf doorverwijst (``search`` -> ``magnifier``,
  ``delete`` -> ``trash``, ``info`` -> ``info-circle``): 328 op 0.8.92. Die renderen
  gewoon, dus ze horen bij de woordenschat.

De alias-regex is daarbij ruim: uit de geminificeerde bundel pikt hij ook elf sleutels op
die geen iconnaam zijn (``type``, ``linux``, ``radiogroup`` en acht andere). Dat maakt de
set iets te groot en nooit te klein, en te groot is hier de veilige kant: deze set
WAARSCHUWT, en de echte poort is ``tests/test_lotc_icon_mapping.py``, die op de
gerenderde markup meet.
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
