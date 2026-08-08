"""Fase 2 van het componentenplan: onze componentnamen naar die van LOTC.

Het einddoel is dat LOTC (Lord of the Components) jinja-roos-components vervangt. Twee
van onze namen heten daar anders. Een hernoeming is alleen veilig als hij vandaag, onder
jinja-roos, hetzelfde blijft renderen - anders is het geen hernoeming maar een wijziging.

Deze tests leggen dat vast:

1. ``c-p`` is hernoemd naar ``c-paragraph`` en komt nergens meer voor. Roos kent beide
   namen (``p`` is een alias van ``paragraph``), dus de omzetting is vandaag al veilig.
2. De twee namen renderen aantoonbaar identiek, voor de vormen die wij aanroepen. Dat is
   het bewijs onder punt 1, niet een aanname erover.
3. ``c-menubar`` is NIET hernoemd naar ``c-menu``, omdat roos geen ``menu`` kent. Die test
   faalt zodra roos hem wel kent - dan is de uitzondering niet meer nodig en kan de tweede
   helft van fase 2 alsnog.
"""

import pathlib
import re
import tempfile

import pytest
from jinja2 import Environment, FileSystemLoader
from jinja_roos_components import setup_components
from jinja_roos_components.registry import ComponentRegistry

from opi.core.templates import CATALOG_DIR, TEMPLATES_DIR

# Een componenttag met naam ``p``: ``<c-p>``, ``<c-p ...>``, ``<c-p/>`` en ``</c-p>``.
OUDE_PARAGRAAFTAG = re.compile(r"</?c-p[\s>/]")

# De vormen waarin wij paragrafen aanroepen. Meer dan wat er in de templates staat, zodat
# de gelijkwaardigheid ook geldt als er straks een attribuut bij komt.
PARAGRAAFVORMEN = [
    "<c-p>tekst</c-p>",
    '<c-p class="rvo-margin-block-end--sm">tekst</c-p>',
    '<c-p size="sm">tekst</c-p>',
    '<c-p color="zwart">tekst</c-p>',
    "<c-p noSpacing>tekst</c-p>",
    '<c-p content="tekst" />',
    "<c-p>tekst met <strong>opmaak</strong> en {{ waarde }}</c-p>",
]


def _templatebestanden() -> list[pathlib.Path]:
    """Alle Jinja-templates van het portaal: de eigen map plus die van de diensten."""
    bestanden: list[pathlib.Path] = []
    for map_ in (TEMPLATES_DIR, CATALOG_DIR):
        bestanden.extend(pathlib.Path(map_).rglob("*.html.j2"))
    return bestanden


def test_oude_paragraaftag_komt_nergens_meer_voor() -> None:
    """``c-p`` is overal ``c-paragraph`` geworden, en dat blijft zo."""
    gevonden = [
        f"{pad}:{nr}"
        for pad in _templatebestanden()
        for nr, regel in enumerate(pad.read_text().splitlines(), start=1)
        if OUDE_PARAGRAAFTAG.search(regel)
    ]
    assert gevonden == [], (
        "Deze templates gebruiken nog <c-p>. LOTC kent die naam niet; schrijf <c-paragraph>, "
        f"wat roos net zo rendert:\n  " + "\n  ".join(gevonden)
    )


@pytest.mark.parametrize("vorm", PARAGRAAFVORMEN)
def test_paragraaf_hernoeming_verandert_de_uitvoer_niet(vorm: str) -> None:
    """``<c-p>`` en ``<c-paragraph>`` leveren onder roos exact dezelfde HTML op."""
    map_ = pathlib.Path(tempfile.mkdtemp())
    (map_ / "oud.html.j2").write_text(vorm)
    (map_ / "nieuw.html.j2").write_text(vorm.replace("c-p", "c-paragraph"))

    env = Environment(loader=FileSystemLoader(str(map_)), autoescape=True)
    setup_components(env, strict_validation=True)

    oud = env.get_template("oud.html.j2").render(waarde="x")
    nieuw = env.get_template("nieuw.html.j2").render(waarde="x")
    assert oud == nieuw


def test_menubar_wacht_op_een_menu_in_roos() -> None:
    """``c-menubar`` heet in LOTC ``menu``, maar roos kent die naam niet.

    Zolang dat zo is, zou de hernoeming de applicatie breken en blijft ``c-menubar`` staan.
    Deze test faalt zodra roos wel een ``menu`` heeft: dan is deze uitzondering weg en kan
    de tweede helft van fase 2 alsnog gedaan worden.
    """
    namen = set(ComponentRegistry().get_all_component_names())
    assert "menubar" in namen
    assert "menu" not in namen, (
        "roos kent nu een 'menu'-component. De uitzondering is niet meer nodig: hernoem "
        "<c-menubar> naar <c-menu> (de LOTC-naam) en verwijder deze test."
    )
