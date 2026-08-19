"""Een ``<c-stack>`` hoort niet RECHTSTREEKS in een ``<c-td>``.

Gemeten op ``/admin/approvals`` in Firefox: de kolom "Laatste wijziging" was een letter
breed - "16 augustus 2026" over veertien regels, een teken per regel - met de rest van de
cel leeg ernaast. Chromium en WebKit toonden dezelfde HTML goed, dus geen enkele bestaande
poort zag het en de HTML zelf was niet verdacht.

De keten: ``<nldd-cell>`` (dat is ``<c-td>``) legt zijn kinderen neer met
``display: flex; flex-direction: column; align-items: flex-start``, dus ze worden op de
dwarsas zo smal als hun eigen inhoud. Daaronder hangt ``div.lotc-stack`` (``<c-stack>``)
met daarin ``nldd-rich-text`` (``<c-paragraph>``), en dat laatste is ``display: grid``.
Firefox rekent de intrinsieke breedte van die flex-in-flex-in-grid-keten uit als 0.

Gemeten welke schakel het is: een ``<c-paragraph>`` ZONDER stack in dezelfde cel is 212px
breed en eenregelig, en een stack met kale ``<p>``-kinderen ook. Alleen de combinatie
stack + rich-text valt om.

De stack voegt daar ook niets toe: een cel IS al een kolom-flexbox, dus zijn kinderen
stapelen vanzelf. Weglaten is dus geen omweg om een browserfout heen maar gewoon minder
markup, en het meet in alle drie de motoren hetzelfde.

Wat er in LOTC zou moeten veranderen staat als punt 14 in ``request_for_components.md``.
Komt dat er, dan kan deze test weg. Het gedrag in de browser wordt gemeten door
``tests/e2e/test_lotc_domeinbeheer.py``; deze test is de goedkope poort eronder, zodat het
patroon niet ergens anders terugkomt.
"""

from __future__ import annotations

import pathlib
import re

from opi.core.template_helpers import CATALOG_DIR, TEMPLATES_DIR

#: Een ``<c-td ...>`` gevolgd door een ``<c-stack``, met alleen witruimte en Jinja-blokken
#: ertussen. Zo staan de gevallen erin die we gemeten hebben; een stack die dieper in de cel
#: zit (in een kaart, in een cluster) is een ander geval en valt niet om.
#: Let op de ``[^<]``: een overgeslagen Jinja-blok mag GEEN tag-grens passeren. Met een kale
#: ``.*?`` onder ``re.DOTALL`` slikte deze regex ``</c-td></c-table>`` heen en landde hij op
#: een stack die helemaal buiten de tabel stond (gemeten op
#: ``_gedeelde-diensten-databases.html.j2``, een valse melding).
STAPEL_IN_CEL = re.compile(
    r"<c-td[^>]*>\s*(?:\{\#[^<]*?\#\}\s*|\{%[^<]*?%\}\s*)*<c-stack",
    re.DOTALL,
)


def _templatebestanden() -> list[pathlib.Path]:
    """Alle Jinja-templates van het portaal: de eigen map plus die van de diensten."""
    bestanden: list[pathlib.Path] = []
    for map_ in (TEMPLATES_DIR, CATALOG_DIR):
        bestanden.extend(pathlib.Path(map_).rglob("*.html.j2"))
    return bestanden


def test_geen_stapel_direct_in_een_tabelcel() -> None:
    """Geen enkel sjabloon zet een ``<c-stack>`` als eerste kind van een ``<c-td>``."""
    gevonden = [str(pad) for pad in _templatebestanden() if STAPEL_IN_CEL.search(pad.read_text())]
    assert gevonden == [], (
        "Deze templates zetten een <c-stack> rechtstreeks in een <c-td>. In Firefox krimpt "
        "die stack tot 0 breed en valt de tekst per teken uiteen. Laat de stack weg: een cel "
        "is zelf al een kolom-flexbox, dus de kinderen stapelen vanzelf.\n  " + "\n  ".join(gevonden)
    )
