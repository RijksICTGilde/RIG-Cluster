"""Jinja in een ``@click``-waarde wordt NIET gerenderd.

Gemeten op ``/admin/approvals``: de knop "Beheren" stond in het sjabloon als

    <c-button ... @click="openApprovalModal('{{ project.project_name }}')" />

en kwam er als

    <nldd-button ... onclick="openApprovalModal('{{ project.project_name }}')">

uit - met de accolades en al. De componentlaag neemt de waarde van een ``@``-afhandelaar
LETTERLIJK uit de bron over en zet hem in het ``onclick``-attribuut; die tekst komt nooit
langs Jinja. Gewone attributen (``label``, ``data-*``, ``type``) gaan er wel doorheen, dus
het verschil is niet te zien aan het sjabloon - alleen aan wat de browser krijgt.

Wat de gebruiker daarvan merkte: de kop van de dialoog las "Domeingoedkeuring -
{{ project.project_name }}", en het formulier werd opgehaald bij
``/admin/approvals/%7B%7B%20project.project_name%20%7D%7D/modal-wizard/admin-approval``.
Geen foutmelding in de log, geen 500 - een 404 op een projectnaam die niemand heeft.

De weg die wel werkt staat in ``features/lotc-bouwlijn.md``: zet de waarde in een gewoon
attribuut (``data-project="{{ ... }}"``) en laat de afhandelaar hem daar uitlezen
(``@click="openApprovalModal(this.dataset.project)"``). Een ``{% set %}``-blok met
``:attrs`` kan ook, maar dat vraagt geneste aanhalingstekens binnen een attribuutwaarde en
daar is deze codebase al vaker op gestruikeld.

Deze test bewaakt de REGEL, niet de ene knop: er hoort nergens Jinja in de waarde van een
``@``-afhandelaar te staan.
"""

from __future__ import annotations

import pathlib
import re

from opi.core.template_helpers import CATALOG_DIR, TEMPLATES_DIR

# Een @-afhandelaar (@click, @change, @input, ...) met een {{ ... }} of {% ... %} in zijn
# waarde. Alleen dubbele aanhalingstekens: zo schrijft dit project zijn attributen.
KLIK_MET_JINJA = re.compile(r"@[a-z]+\s*=\s*\"[^\"]*\{[{%]")


def _templatebestanden() -> list[pathlib.Path]:
    """Alle Jinja-templates van het portaal: de eigen map plus die van de diensten."""
    bestanden: list[pathlib.Path] = []
    for map_ in (TEMPLATES_DIR, CATALOG_DIR):
        bestanden.extend(pathlib.Path(map_).rglob("*.html.j2"))
    return bestanden


def test_geen_jinja_in_de_waarde_van_een_klikafhandelaar() -> None:
    """Geen enkel sjabloon zet een Jinja-expressie in een ``@``-afhandelaar."""
    gevonden = [
        f"{pad}:{nr}: {regel.strip()[:120]}"
        for pad in _templatebestanden()
        for nr, regel in enumerate(pad.read_text().splitlines(), start=1)
        if KLIK_MET_JINJA.search(regel)
    ]
    assert gevonden == [], (
        "Deze @-afhandelaars bevatten Jinja. Die waarde wordt letterlijk in het "
        "onclick-attribuut gezet en nooit gerenderd, dus komen de accolades in de browser "
        "terecht. Zet de waarde in een data-attribuut en lees hem uit met "
        "this.dataset.<naam>:\n  " + "\n  ".join(gevonden)
    )
