"""Een icoon hoort NAAST een kop, niet erin.

Gemeld op de goedkeuringsdialoog van ``/admin/approvals``: het vinkje voor
"Domeingoedkeuring - <project>" stond net niet op dezelfde lijn als de titel. Het sjabloon
schreef het icoon als kind van de kop::

    <c-heading type="h2" size="4" id="approval-title">
        <c-icon icon="check-mark-circle" size="lg" />
        <span id="approval-title-text">Domeingoedkeuring</span>
    </c-heading>

Dat rendert naar een ``<nldd-icon>`` binnen de ``<h2>``, en daar is het een INLINE element
met een vaste maat. Inline betekent uitlijnen op de BASISLIJN van de tekst, en de
basislijn van een kop ligt onder zijn kapitaalhoogte: het icoon staat dus met zijn
onderkant op de plek waar de letters op staan, terwijl zijn midden hoger uitkomt dan het
midden van de letters. Het verschil groeit met de maat van het icoon, en het is geen
maatprobleem - een kleiner icoon staat even scheef, alleen minder ver.

Een kop legt zijn kinderen niet naast elkaar en heeft geen knop om ze te centreren. Het
antwoord is dus geen eigen CSS-regel die het thema corrigeert - dat is een pleister op
verkeerd gebruik en hij blijft achter zodra het thema verandert - maar de constructie die
er al is: icoon en kop als BROERS in een cluster dat verticaal centreert.

    <c-cluster gap="sm" align="center">
        <c-icon icon="check-mark-circle" size="lg" />
        <c-heading type="h2" size="4" label="Domeingoedkeuring" />
    </c-cluster>

Zo staat het in ``bg/project-tabs.html.j2``, ``bg/cli.html.j2``, ``bg/_wizard-step.html.j2``
en sinds de reparatie van de bewerkdialoog ook in ``bg/_modals.html.j2``. Wat de kop verder
draagt blijft op de kop staan: het ``id`` waar ``aria-labelledby`` naar wijst, en de
``<span>`` waar JavaScript de projectnaam in schrijft.

Deze test is de goedkope poort. Wat de browser er werkelijk van maakt wordt gemeten in
``tests/e2e/test_lotc_domeinbeheer.py``, dat de verticale middens van icoon en titel naast
elkaar legt.
"""

from __future__ import annotations

import pathlib
import re

from opi.core.template_helpers import CATALOG_DIR, TEMPLATES_DIR

#: Jinja-commentaar. Dat wordt eruit gehaald voordat er gezocht wordt: ``bg/_modals.html.j2``
#: LEGT dit patroon uit in een commentaarblok, met de tags erbij, en zou anders zichzelf
#: aangeven terwijl de markup eronder juist de goede vorm heeft.
COMMENTAAR = re.compile(r"\{#.*?#\}", re.DOTALL)

#: Een openende ``<c-heading ...>`` die NIET zichzelf sluit. De attribuutwaarden worden
#: overgeslagen zodat een ``>`` binnen een waarde de tag niet vroegtijdig afkapt.
KOP_OPENT = re.compile(r"<c-heading\b((?:[^>\"']|\"[^\"]*\"|'[^']*')*?)>")

#: Een kale kop uit handgeschreven HTML. Die staat er ook: een sjabloon dat uit de oude boom
#: is overgenomen kan een ``<h2>`` dragen in plaats van een ``<c-heading>``.
KALE_KOP = re.compile(r"<(h[1-6])\b[^>]*>(.*?)</\1>", re.DOTALL)

#: Een icoon, in beide schrijfwijzen: het component en de webcomponent waar het naar rendert.
ICOON = re.compile(r"<c-icon\b|<nldd-icon\b")


def _templatebestanden() -> list[pathlib.Path]:
    """Alle Jinja-templates van het portaal: de eigen map plus die van de diensten."""
    bestanden: list[pathlib.Path] = []
    for map_ in (TEMPLATES_DIR, CATALOG_DIR):
        bestanden.extend(pathlib.Path(map_).rglob("*.html.j2"))
    return bestanden


def _iconen_in_koppen(bron: str) -> list[int]:
    """De regelnummers waarop een kop een icoon als kind heeft."""
    zonder_commentaar = COMMENTAAR.sub(lambda m: "\n" * m.group(0).count("\n"), bron)

    regels: list[int] = []
    for opening in KOP_OPENT.finditer(zonder_commentaar):
        if opening.group(1).rstrip().endswith("/"):
            continue
        einde = zonder_commentaar.find("</c-heading>", opening.end())
        if einde == -1:
            continue
        if ICOON.search(zonder_commentaar[opening.end() : einde]):
            regels.append(zonder_commentaar[: opening.start()].count("\n") + 1)

    for kale in KALE_KOP.finditer(zonder_commentaar):
        if ICOON.search(kale.group(2)):
            regels.append(zonder_commentaar[: kale.start()].count("\n") + 1)

    return sorted(regels)


def test_geen_icoon_binnen_een_kop() -> None:
    """Geen enkel sjabloon zet een icoon als kind van een kop."""
    gevonden: list[str] = []
    for pad in sorted(_templatebestanden()):
        gevonden.extend(f"{pad}:{regel}" for regel in _iconen_in_koppen(pad.read_text()))

    assert gevonden == [], (
        "Deze plekken zetten een icoon BINNEN een kop. Daar is het een inline element dat "
        "op de tekstbasislijn uitlijnt, dus het staat niet op dezelfde lijn als de titel - "
        "en dat wordt niet met een eigen CSS-regel rechtgezet. Zet icoon en kop als broers "
        "in een cluster dat centreert:\n"
        '    <c-cluster gap="sm" align="center">\n'
        '        <c-icon icon="..." size="lg" />\n'
        '        <c-heading type="h2" size="4" label="..." />\n'
        "    </c-cluster>\n"
        "Laat een id, aria-attribuut of <span> waar JavaScript aan hangt op de KOP staan.\n  "
        + "\n  ".join(gevonden)
    )
