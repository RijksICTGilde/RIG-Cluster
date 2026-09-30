"""De kleur van de voortgangsbalk per uitkomst, en waarom LOTC_STRICT die niet dekt.

``LOTC_STRICT=1`` weigert een attribuutWAARDE die niet in de lijst van het component staat.
Alleen: het doet dat bij het COMPILEREN, dus alleen voor een waarde die letterlijk in het
sjabloon staat. Een berekende waarde gaat er ongezien langs::

    color="error"                  -> ComponentError: Invalid value 'error'
    color="{{ 'error' }}"          -> gaat er stil door
    :color="'error'"               -> gaat er stil door

Precies zo stond het hier: de balk van een MISLUKTE taak vroeg ``color="error"`` via een
ternary, en ``error`` bestaat niet in deze lijst. Het component valt dan terug op zijn
standaard, dus de drie uitkomsten kunnen op het scherm samenvallen, en er komt nergens een
melding voorbij. Strict haalde de LITERALE gevallen boven water (``value-display="true"``,
``color="info"``) en dit geval niet.

Daarom staat de meting hier, en niet op de tekst van het sjabloon: elke kleur die deze
balken vragen wordt langs de compiler gehaald als LITERAAL, want dat is de enige stand
waarin hij zijn eigen lijst nakijkt. Die lijst wordt dus niet hier overgeschreven; hij
blijft van het component.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from lord_of_the_components.extension import ComponentError
from opi.core.templates_lotc import templates_lotc

#: De twee sjablonen die hun balkkleur uit de status berekenen.
SJABLONEN = [
    "bg/_task-progress.html.j2",
    "bg/_modal-wizard-progress-fragment.html.j2",
]

#: Wat een mislukking, een afronding en een lopende taak op het scherm moeten schelen.
#: Niet de namen zelf zijn het punt (die horen bij het thema), maar dat ze bestaan en dat
#: de drie uitkomsten er niet hetzelfde uitzien.
STATUSSEN = ["running", "completed", "failed"]

VOORTGANGSBALK = re.compile(r"<nldd-progress-bar\b[^>]*>")
KLEUR = re.compile(r'\scolor="([^"]*)"')


def _context(status: str) -> dict[str, Any]:
    return {
        "task_id": "t1",
        "project_name": "proj",
        "progress": 42,
        "current_step": "Bezig",
        "tasks": [{"name": "Uitrollen", "status": status, "error": None, "subtasks": []}],
        "status": status,
        "error": "Kon niet verbinden" if status == "failed" else None,
        "progress_url": "/x",
        "container_id": "c1",
    }


def _balk(sjabloon: str, status: str) -> str:
    html = templates_lotc.env.get_template(sjabloon).render(**_context(status))
    treffer = VOORTGANGSBALK.search(html)
    assert treffer, f"{sjabloon} bij status={status} rendert geen voortgangsbalk"
    return treffer.group(0)


def _accepteert_de_compiler(attribuut: str, waarde: str) -> bool:
    """Of het component deze waarde kent, gevraagd aan de compiler zelf.

    Als LITERAAL, want dat is de enige vorm waarin hij zijn lijst nakijkt. Zo hoeft de
    lijst met toegestane waarden hier niet te staan: een nieuwe themaversie die er iets
    aan verandert, verandert deze meting mee.
    """
    bron = f'<c-progress-bar text="V" :value="42" max="100" {attribuut}="{waarde}" accessible-label="x" />'
    try:
        templates_lotc.env.from_string(bron).render()
    except ComponentError:
        return False
    return True


def test_de_compiler_keurt_een_onbekende_kleur_af() -> None:
    """De meetlat zelf. Keurt hij niets meer af, dan zeggen de toetsen eronder niets."""
    assert _accepteert_de_compiler("color", "accent")
    assert not _accepteert_de_compiler("color", "error"), (
        "LOTC_STRICT staat niet aan, of het component kent 'error' nu wel; in het eerste "
        "geval meet dit bestand niets (zie tests/conftest.py)"
    )


@pytest.mark.parametrize("sjabloon", SJABLONEN)
@pytest.mark.parametrize("status", STATUSSEN)
def test_de_gevraagde_kleur_bestaat_echt(sjabloon: str, status: str) -> None:
    """Een naam die het component niet kent valt terug op de standaardkleur, stil."""
    balk = _balk(sjabloon, status)
    treffer = KLEUR.search(balk)

    assert treffer, f"{sjabloon} bij status={status} zet geen color op de balk: {balk}"
    assert _accepteert_de_compiler("color", treffer.group(1)), (
        f"{sjabloon} vraagt bij status={status} om color={treffer.group(1)!r}, en die kleur "
        "kent het component niet. Omdat de waarde berekend is, laat LOTC_STRICT hem door en "
        "krijgt de balk de standaardkleur."
    )


@pytest.mark.parametrize("sjabloon", SJABLONEN)
def test_een_mislukte_taak_ziet_er_niet_uit_als_een_lopende(sjabloon: str) -> None:
    """De schade van de vorige fout: drie uitkomsten, een kleur.

    Een onbekende naam valt terug op de standaard van het component, dus mislukt en nog
    bezig komen op hetzelfde uit. Deze toets vraagt niet WELKE kleuren het zijn, alleen
    dat de drie uitkomsten te onderscheiden zijn.
    """
    kleuren = {status: KLEUR.search(_balk(sjabloon, status)).group(1) for status in STATUSSEN}  # type: ignore[union-attr]

    assert len(set(kleuren.values())) == len(STATUSSEN), f"{sjabloon} geeft deze uitkomsten dezelfde kleur: {kleuren}"


@pytest.mark.parametrize("sjabloon", SJABLONEN)
def test_het_percentage_staat_in_de_balk(sjabloon: str) -> None:
    """``value-display="true"`` bestaat niet en liet het percentage helemaal weg.

    Dit is de literale kant en die staat onder de compiler, maar hij staat hier bij zijn
    tegenhanger: allebei gaan ze over de vraag of deze balk iets zegt.
    """
    balk = _balk(sjabloon, "running")

    assert 'value-display="inline"' in balk, f"het percentage staat niet in de balk: {balk}"
