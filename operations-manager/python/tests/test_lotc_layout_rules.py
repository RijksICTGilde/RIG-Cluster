"""Bewaakt de indelingsregel waar de spacing telkens op misging.

In dit componentensysteem bestaat een gap alleen waar een **stack de ouder is**. Een
``<c-card>`` is geen stack: hij zet zijn kinderen achter elkaar in een container. Wie dus
dit schrijft:

    <c-card>
        <c-section-head title="Mijn projecten" />
        <c-stack gap="md"> ... </c-stack>
    </c-card>

krijgt een kop die tegen zijn eigen inhoud aan plakt. De stack regelt de afstand tussen
zijn kinderen, niet die tussen zichzelf en de kop erboven.

Dat is geen vergissing die je een keer maakt maar een die je op elke kaart opnieuw maakt,
en hij is op een screenshot subtiel genoeg om door te glippen. Vandaar deze test in
plaats van een afspraak: de hertekende pagina's bouwen hun kaarten via ``panel()`` uit
bg/_patronen.html.j2, waar de kop binnen de stack zit.
"""

import re
from pathlib import Path

BG_DIR = Path(__file__).parent.parent / "opi" / "templates_lotc" / "bg"

# Een kaart met een kopregel er los in: precies het patroon dat de kop laat plakken.
CARD_WITH_LOOSE_HEAD = re.compile(r"<c-card\b[^>]*>\s*<c-section-head", re.DOTALL)


def test_cards_are_built_with_the_panel_macro() -> None:
    """Geen enkele hertekende pagina zet een kopregel los in een kaart."""
    offenders = [
        path.name
        for path in sorted(BG_DIR.glob("*.j2"))
        if not path.name.startswith("_") and CARD_WITH_LOOSE_HEAD.search(path.read_text())
    ]
    assert not offenders, (
        f"kopregel los in een c-card (gebruik panel() uit bg/_patronen.html.j2): {offenders}. "
        "Een kaart is geen stack, dus kop en inhoud raken elkaar."
    )


def test_pages_use_the_shared_patterns() -> None:
    """Elke hertekende pagina importeert de patronen, zodat ze ook te gebruiken zijn."""
    missing = [
        path.name
        for path in sorted(BG_DIR.glob("*.j2"))
        if not path.name.startswith("_") and "bg/_patronen.html.j2" not in path.read_text()
    ]
    assert not missing, f"gebruikt de gedeelde patronen niet: {missing}"


def test_spacing_uses_the_design_system_scale() -> None:
    """Geen eigen rem-waarden voor gaps; die lopen onderling uiteen en volgen het thema niet.

    Het LOTC-project gebruikt in zijn eigen bg-templates ``gap="lg"`` en ``gap="md"``.
    Losse waarden zien er per pagina redelijk uit, maar tussen pagina's verschillen ze, en
    ze schuiven niet mee als het thema zijn maten aanpast.
    """
    offenders: dict[str, list[str]] = {}
    for path in sorted(BG_DIR.glob("*.j2")):
        raw_gaps = re.findall(r'gap="(\d[^"]*)"', path.read_text())
        if raw_gaps:
            offenders[path.name] = raw_gaps
    assert not offenders, f"eigen rem-waarden in plaats van de schaal: {offenders}"
