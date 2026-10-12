"""De stand per stap in de stappenbalk, en waarom LOTC_STRICT die niet dekt.

``LOTC_STRICT=1`` weigert een attribuutWAARDE die niet in de lijst van het component staat,
maar het kijkt die lijst na bij het COMPILEREN. Dus alleen voor een waarde die letterlijk
in het sjabloon staat; een berekende waarde gaat er ongezien langs::

    status="complete"                   -> ComponentError: Invalid value 'complete'
    status="{{ 'complete' }}"           -> gaat er stil door
    :status="step.status or none"       -> gaat er stil door

Precies zo stond het hier: de proefopstelling van de wizard gaf ``complete`` mee en de enum
van step-indicator-item is past/current/future. Het component valt dan terug op zijn
standaard, dus een afgeronde stap ziet eruit als een stap waar nog niets aan gedaan is, en
er komt nergens een melding voorbij. Gevonden met de sweep van stap 8, niet door een toets.

Daarom staat de meting hier en niet op de tekst van de sjablonen: elke stand die een
stappenbalk op het scherm zet wordt als LITERAAL langs de compiler gehaald, want dat is de
enige stand waarin hij zijn eigen lijst nakijkt. Die lijst wordt dus niet in dit bestand
overgeschreven, hij blijft van het component.

Alle drie de balken staan erin: de proefopstelling van /lotc/wizard, en de twee die de
echte wizard tekent. Bij die laatste twee leest de compiler wel mee op de afgeronde stap,
want daar staat ``status="past"`` letterlijk in het sjabloon; de andere tak rekent hij uit
met ``{{ ... }}`` en die gaat er even ongezien langs als de eerste. Gemeten: ``past`` naar
``complete`` is een ComponentError bij het compileren, de tak eronder komt alleen hier
boven water.

Welke sjablonen in BALKEN horen is geen lijst die hier met de hand bijgehouden wordt; die
vraag stelt ``test_balken_kent_elk_sjabloon_dat_een_stand_berekent`` aan de sjablonen op
schijf. Anders veroudert juist deze toets op de manier die hij meet: een nieuw sjabloon
valt er stil buiten.
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from lord_of_the_components.extension import ComponentError
from opi.core.templates_lotc import templates_lotc
from opi.web.lotc_fixtures import page_data
from tests.berekende_attribuutwaarden import sjablonen_met_berekende_waarde

STAP = re.compile(r"<nldd-step-indicator-item\b[^>]*>")
STAND = re.compile(r'\sstatus="([^"]*)"')


def _request() -> SimpleNamespace:
    return SimpleNamespace(cookies={}, url=SimpleNamespace(path="/lotc/wizard"), state=SimpleNamespace(csrf_token="t"))


def _wizard_steps() -> SimpleNamespace:
    """Een halfafgelopen wizard: stap 1 af, stap 2 aan de hand, stap 3 nog niet."""
    return SimpleNamespace(
        all=["project", "diensten", "componenten"],
        current="diensten",
        completed={"project"},
        titles={"project": "Project", "diensten": "Diensten", "componenten": "Componenten"},
        index=1,
        count=3,
    )


def _proefopstelling() -> str:
    return templates_lotc.env.get_template("bg/wizard.html.j2").render(
        request=_request(), navigation={}, menu_items=[], **page_data("wizard")
    )


def _echte_wizard(sjabloon: str) -> str:
    return templates_lotc.env.get_template(sjabloon).render(request=_request(), steps=_wizard_steps(), flow_id="f1")


#: Elke plek die een <c-step-indicator-item> tekent met een stand die de compiler niet leest.
BALKEN: dict[str, Any] = {
    "bg/wizard.html.j2": _proefopstelling,
    "wizard/wizard_steps_indicator.html.j2": lambda: _echte_wizard("wizard/wizard_steps_indicator.html.j2"),
    "bg/_wizard-steps.html.j2": lambda: _echte_wizard("bg/_wizard-steps.html.j2"),
}


def _standen(naam: str) -> list[str]:
    """De standen zoals ze in de uitvoer staan.

    Een stap zonder ``status`` is geldig: die leidt zijn stand af uit ``current`` op de
    balk. Wat hier terugkomt zijn de standen die wel gevraagd zijn.
    """
    html = BALKEN[naam]()
    stappen = STAP.findall(html)
    assert stappen, f"{naam} rendert geen enkele stap"

    standen = [treffer.group(1) for treffer in (STAND.search(stap) for stap in stappen) if treffer]
    assert standen, f"{naam} zet op geen enkele stap een stand, dus deze toets meet niets: {stappen}"
    return standen


def _accepteert_de_compiler(waarde: str) -> bool:
    """Of het component deze stand kent, gevraagd aan de compiler zelf.

    Als LITERAAL, want dat is de enige vorm waarin hij zijn lijst nakijkt. Zo hoeft de lijst
    met toegestane standen hier niet te staan: een nieuwe themaversie die er iets aan
    verandert, verandert deze meting mee.
    """
    bron = (
        '<c-step-indicator current="1" accessible-label="x">'
        f'<c-step-indicator-item text="S" status="{waarde}" />'
        "</c-step-indicator>"
    )
    try:
        templates_lotc.env.from_string(bron).render()
    except ComponentError:
        return False
    return True


def test_de_compiler_keurt_een_onbekende_stand_af() -> None:
    """De meetlat zelf. Keurt hij niets meer af, dan zeggen de toetsen eronder niets."""
    assert _accepteert_de_compiler("past")
    assert not _accepteert_de_compiler("complete"), (
        "LOTC_STRICT staat niet aan, of het component kent 'complete' nu wel; in het eerste "
        "geval meet dit bestand niets (zie tests/conftest.py)"
    )


@pytest.mark.parametrize("naam", list(BALKEN))
def test_elke_gevraagde_stand_bestaat_echt(naam: str) -> None:
    """Een stand die het component niet kent valt terug op de standaard, stil."""
    for stand in _standen(naam):
        assert _accepteert_de_compiler(stand), (
            f"{naam} vraagt om status={stand!r}, en die stand kent het component niet. Omdat "
            "de waarde berekend is, laat LOTC_STRICT hem door en krijgt de stap de standaard."
        )


def test_de_proefopstelling_toont_een_afgeronde_stap() -> None:
    """De schade van de vorige fout: /lotc/wizard had geen afgeronde stap meer.

    De pagina is er om de stappenbalk in samenhang te laten zien, en dan hoort er een stap
    achter je te liggen. Met ``complete`` was dat er geen enkele.
    """
    standen = _standen("bg/wizard.html.j2")

    assert "past" in standen, f"geen enkele stap staat op 'past', dus de balk toont geen voortgang: {standen}"


def test_balken_kent_elk_sjabloon_dat_een_stand_berekent() -> None:
    """De lijst hierboven tegen de sjablonen op schijf.

    Een stand die de compiler niet leest komt nergens als fout voorbij, dus een sjabloon
    dat buiten BALKEN valt is precies het geval dat dit bestand hoort te vangen. Een naam
    die eraf moet is even goed een melding waard: dan rendert de toets een sjabloon dat
    zijn stand inmiddels letterlijk zet, en staat de meting op de verkeerde plek.
    """
    gemeten = sjablonen_met_berekende_waarde("c-step-indicator-item", "status")

    assert gemeten == set(BALKEN), (
        f"BALKEN en de sjablonen lopen uiteen. Niet gedekt: {sorted(gemeten - set(BALKEN))}; "
        f"in BALKEN maar zonder berekende stand: {sorted(set(BALKEN) - gemeten)}"
    )
