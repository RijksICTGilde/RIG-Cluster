"""Welke van onze sjablonen een attribuutwaarde BEREKENEN, gedeeld door de toetsen erop.

``LOTC_STRICT=1`` weigert een enum-waarde die niet in de lijst van het component staat,
maar alleen als LITERAAL in het sjabloon: een berekende waarde gaat er ongezien langs en
het component valt dan stil terug op zijn standaard. De toetsen die dat gat dichten
(``test_lotc_stappenbalk_stand.py`` voor de stand van een stap,
``test_lotc_voortgangsbalk_kleur.py`` voor de kleur van een balk) dragen elk een lijst
sjablonen die ze rendert.

Zo'n lijst met de hand bijhouden is precies de fout die deze toetsen meten: hij veroudert
zonder onwaar te worden. Een nieuw sjabloon dat dezelfde berekende waarde zet, valt er
buiten en niemand merkt het. Deze module levert de meting waarmee zo'n lijst zich tegen de
sjablonen op schijf laat houden.
"""

from __future__ import annotations

import re
from pathlib import Path

from opi.core.templates_lotc import CATALOG_DIR, TEMPLATES_LOTC_DIR

#: Onze eigen sjabloonwortels, precies de twee die de env naast de componenten van LOTC
#: zelf kent. Die van LOTC staan er bewust niet bij: die DEFINIEREN de componenten, ze
#: roepen ze niet aan, dus elke attribuutwaarde daar is de lijst en niet een gebruik ervan.
WORTELS = [Path(TEMPLATES_LOTC_DIR), Path(CATALOG_DIR)]


def _tags(tekst: str, tag: str) -> list[str]:
    """Elke ``<tag ...>`` uit de tekst, ook als hij over meer regels loopt.

    Een ``>`` binnen een aanhalingsteken sluit de tag niet. Een attribuutwaarde is een
    expressie of JSON, dus hij kan er een in hebben, en dan zou de tag halverwege eindigen.
    """
    patroon = re.compile(rf"<{re.escape(tag)}\b(?:\"[^\"]*\"|'[^']*'|[^>])*>")
    return patroon.findall(tekst)


def _berekent(tag: str, attribuut: str) -> bool:
    """Of deze tag ``attribuut`` zet op een manier die de compiler niet kan nakijken.

    Twee vormen: ``:attr="expr"`` (een expressie) en ``attr="{{ ... }}"`` (Jinja in een
    gewone waarde), elk met beide soorten aanhalingstekens. Een waarde die via een
    ``:attrs``-spread binnenkomt staat hier niet in; die is aan de tag niet te zien en komt
    uit de sweep.
    """
    if re.search(rf"\s:{re.escape(attribuut)}\s*=", tag):
        return True
    waarde = re.search(rf"\s{re.escape(attribuut)}\s*=\s*(\"[^\"]*\"|'[^']*')", tag)
    return waarde is not None and ("{{" in waarde.group(1) or "{%" in waarde.group(1))


def sjablonen_met_berekende_waarde(tag: str, attribuut: str) -> set[str]:
    """Onze sjablonen die ``tag`` tekenen met een berekende ``attribuut``-waarde.

    De namen komen terug zoals de env ze laadt (relatief aan de wortel), zodat ze naast
    de lijst van een toets te leggen zijn.
    """
    gevonden: set[str] = set()
    for wortel in WORTELS:
        for pad in wortel.rglob("*.j2"):
            tekst = pad.read_text(encoding="utf-8")
            if any(_berekent(gevonden_tag, attribuut) for gevonden_tag in _tags(tekst, tag)):
                gevonden.add(pad.relative_to(wortel).as_posix())
    return gevonden
