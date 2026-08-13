"""Een ``{% include %}`` naar een sjabloon dat niet bestaat is een pagina die niet rendert.

Jinja lost een include pas op als de regel WORDT UITGEVOERD. Een pagina met een verkeerde
naam erin compileert dus prima, staat groen in elke test die het bestand alleen LEEST, en
gaat pas stuk op het moment dat iemand hem opvraagt. Staat die pagina bovendien op een
route die niemand meer gebruikt, dan gaat hij nooit stuk - en dan is de enige manier om
te weten dat hij dood is, dat je het opmeet.

Dat is precies wat er gebeurd was. ``project-details.html.j2`` includeerde
``project-details/section-pending-rollout.html.j2``, een bestand dat in geen enkel
zoekpad bestond. De pagina kon dus al een tijd niet renderen zonder dat iets dat merkte -
en dat was het bewijs dat de hele map dood was, want een pagina die WEL gerenderd werd
had een 500 opgeleverd.

Deze poort maakt dat luid. Elk sjabloon in de boom wordt gelezen, elke naam die het
noemt in ``extends``, ``include``, ``import`` of ``from`` wordt opgezocht in dezelfde
Jinja-omgeving als de applicatie gebruikt (dus inclusief de zoekpaden van de
componentbibliotheek), en een naam die daar niet oplost is een bevinding.

Wat hij NIET kan: een naam die uit een variabele wordt opgebouwd
(``{% include "widgets/" ~ soort %}``). Die staat hieronder als uitzondering, met de
plek waar de mogelijke waarden vandaan komen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from jinja2 import TemplateNotFound
from opi.core.templates_lotc import templates_lotc

#: ``extends``/``include``/``import``/``from`` met een naam die letterlijk in het sjabloon
#: staat. Een naam die uit een expressie komt matcht bewust niet: daar kan deze poort
#: niets zinnigs over zeggen.
VERWIJZING = re.compile(r"""\{%-?\s*(?:extends|include|import|from)\s+['"]([^'"]+\.j2)['"]""")

#: De sjablonen van de applicatie zelf: opi/templates_lotc/ en de dienstpakketten onder
#: opi/services/catalog/. Dat zijn de eerste twee zoekpaden van de omgeving; de rest komt
#: uit de componentbibliotheek en is niet van ons.
BRONMAPPEN = tuple(Path(pad) for pad in templates_lotc.env.loader.searchpath[:2])


def _sjablonen() -> list[Path]:
    gevonden: list[Path] = []
    for map_ in BRONMAPPEN:
        gevonden.extend(sorted(map_.rglob("*.j2")))
    return gevonden


@pytest.mark.parametrize("pad", _sjablonen(), ids=lambda pad: pad.name)
def test_elke_verwijzing_lost_op(pad: Path) -> None:
    ontbreekt: list[str] = []
    for naam in VERWIJZING.findall(pad.read_text(encoding="utf-8")):
        try:
            templates_lotc.env.get_template(naam)
        except TemplateNotFound:
            ontbreekt.append(naam)

    assert ontbreekt == [], (
        f"{pad.name} verwijst naar sjablonen die niet bestaan: {ontbreekt}. "
        "Zolang die regel niet wordt uitgevoerd merkt niemand het; wordt hij dat wel, "
        "dan is het een 500."
    )
