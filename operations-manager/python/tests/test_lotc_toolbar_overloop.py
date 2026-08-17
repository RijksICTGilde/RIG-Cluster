"""Geen eigen overloopgroep in een `nldd-toolbar`.

Gemeld door de eigenaar op /projects: het sorteren stond dubbel op het scherm, en de
hamburgerversie sorteerde niet.

Gemeten in Chromium: de enkele AANWEZIGHEID van een `<nldd-menu-group slot="overflow">`
laat de toolbar permanent zijn "Meer"-knop tonen, ook op 1440px waar alles ruim past.
Zonder die groep blijft die knop verborgen tot het niet meer past. En de items in de groep
worden nooit bruikbaar: na een klik op "Meer" meten ze 0x0 en loopt een klik af op een
timeout.

Het kost niets om hem weg te halen, want de overloop van dit component werkt sowieso niet:
zonder de groep verschijnt op 1024px wel een "Meer"-knop maar is zijn menu leeg. De volledige
meting en het voorstel staan in request_for_components.md.

Een BRONtest en geen browsertest: dit gaat over een patroon dat niet terug moet komen, en dat
is in de sjablonen te zien. De browsermeting die de reden vaststelde is eenmalig gedaan; hem
elke run herhalen zou het component testen in plaats van onze keuze.
"""

from pathlib import Path

import opi

SJABLONEN = Path(opi.__file__).parent / "templates_lotc"


def test_geen_enkel_sjabloon_schrijft_een_eigen_overloopgroep() -> None:
    verdacht = [
        f"{pad.relative_to(SJABLONEN)}:{nr}"
        for pad in SJABLONEN.rglob("*.j2")
        for nr, regel in enumerate(pad.read_text().splitlines(), 1)
        # Op de OPENINGSTAG en niet op de losse tekst, anders slaat hij aan op het
        # commentaar dat uitlegt waarom de groep weg is.
        if '<nldd-menu-group slot="overflow"' in regel
    ]

    assert verdacht == [], (
        "Een eigen slot=\"overflow\"-groep laat nldd-toolbar zijn 'Meer'-knop permanent "
        "tonen, zodat de knop ernaast er dubbel bij staat, en de items erin worden nooit "
        "klikbaar. Zie request_for_components.md:\n  " + "\n  ".join(verdacht)
    )
