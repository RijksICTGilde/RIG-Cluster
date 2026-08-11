"""De foutmelding bij een formulierveld: staat hij er, en is hij te ZIEN?

Gemeten in een browser stond de melding wel in de DOM - twee
``<nldd-form-field-error-text>`` met de juiste tekst - en was hij ``display: none`` met
hoogte 0. De gebruiker zag een rood kader en niet wat er mis was.

De oorzaak zit in het samenspel tussen ``nldd-form-field`` en het invoerveld: het
component toont alleen de foutregels waarvan het id in ``error-message`` OP HET
INVOERVELD staat, en lotc-forms schrijft daar ``error-message-ids`` - het attribuut dat
``nldd-form-field`` zelf gebruikt om ``aria-describedby`` te bedraden. Zie
``opi/forms/lotc_attrs.py`` (bedraad_foutmelding) voor de volledige meting.

Wat hier bewaakt wordt:

- de bedrading zelf, op de HTML die de veldsjablonen opleveren (elk soort veld);
- dat onze kopie van ``components/_forms.j2`` op EEN punt van de geinstalleerde afwijkt,
  zodat een nieuwe versie van lotc-forms hier opvalt en niet stilletjes langs ons heen
  gaat.

Dat de melding in een BROWSER ook echt hoogte heeft, staat in
``tests/e2e/test_lotc_veldfout_zichtbaar.py``: dat is de meting waar deze reparatie
vandaan komt.
"""

from __future__ import annotations

import re
from pathlib import Path

import lotc_forms
import pytest
from opi.core.templates_lotc import templates_lotc
from opi.forms.lotc_attrs import bedraad_foutmelding

#: De veldsoorten die via ``nldd_field`` renderen, met hun tag voor het invoerelement.
VELDEN = [
    ("c-text-input-field", "nldd-text-field"),
    ("c-textarea-field", "nldd-multi-line-text-field"),
    ("c-date-input-field", "nldd-date-field"),
    ("c-file-input-field", "nldd-file-field"),
    ("c-select-field", "nldd-combo-box"),
    ("c-radio-button-field", "div"),
]


def _render(tag: str, *, error: str | None = "Dit veld is verplicht") -> str:
    fout = f' error="{error}"' if error else ""
    bron = f'<{tag} id="veld" name="veld" label="Naam"{fout}/>'
    return templates_lotc.env.from_string(bron).render()


@pytest.mark.parametrize(("tag", "besturing"), VELDEN)
def test_fout_bedraadt_het_invoerveld(tag: str, besturing: str) -> None:
    """Het invoerveld draagt invalid plus error-message met het id van de foutregel."""
    html = _render(tag)
    assert f"<{besturing} " in html, f"{tag} rendert geen {besturing}"
    assert 'error-message="veld-error"' in html, f"{tag} mist error-message"
    assert 'id="veld-error"' in html, f"{tag} mist de foutregel zelf"
    # error-message-ids is de kant OP HET VELD die nldd-form-field zelf zet; als het
    # sjabloon hem schrijft wint hij het van niets en verliest de foutregel zijn id.
    assert "error-message-ids" not in html, f"{tag} schrijft nog in error-message-ids"


@pytest.mark.parametrize(("tag", "besturing"), VELDEN)
def test_fout_is_ook_voor_een_schermlezer_zichtbaar(tag: str, besturing: str) -> None:
    """aria-invalid staat op de besturing; zonder dat is de fout er alleen visueel."""
    del besturing
    assert 'aria-invalid="true"' in _render(tag)


@pytest.mark.parametrize(("tag", "besturing"), VELDEN)
def test_zonder_fout_geen_bedrading(tag: str, besturing: str) -> None:
    """Een veld zonder fout blijft precies zoals het was."""
    del besturing
    html = _render(tag, error=None)
    assert "error-message" not in html
    assert "aria-invalid" not in html
    assert "form-field-error-text" not in html


def test_bedrading_laat_een_bestaande_invalid_staan() -> None:
    """Wat het sjabloon zelf al zette wordt niet verdubbeld."""
    uit = str(bedraad_foutmelding('<nldd-text-field invalid error-message-ids="x-error"></nldd-text-field>', "x-error"))
    assert uit.count("invalid") == 2, uit  # invalid + aria-invalid, niet meer
    assert uit.count("aria-invalid") == 1
    assert "error-message-ids" not in uit


def test_bedrading_raakt_alleen_het_eerste_element() -> None:
    """De foutregel eronder is geen invoerveld en blijft ongemoeid."""
    uit = str(bedraad_foutmelding("<div><span>een</span></div><div>twee</div>", "x-error"))
    assert uit.startswith('<div invalid aria-invalid="true" error-message="x-error">')
    assert uit.endswith("<div>twee</div>")


def test_bedrading_escapet_het_id() -> None:
    """Een id komt uit een veldpad; aanhalingstekens mogen de tag niet openbreken."""
    uit = str(bedraad_foutmelding("<input>", 'x"><script>alert(1)</script>'))
    assert "<script>" not in uit
    assert "&quot;" in uit


def test_bedrading_op_lege_invoer() -> None:
    """Zonder element valt er niets te bedraden en komt de invoer terug zoals hij was."""
    assert str(bedraad_foutmelding("   ", "x-error")) == "   "


# --------------------------------------------------------------------------------------
# De kopie naast het origineel
# --------------------------------------------------------------------------------------

#: Onze kopie van de gedeelde macro's van lotc-forms.
ONZE_KOPIE = Path(__file__).resolve().parent.parent / "opi" / "templates_lotc" / "components" / "_forms.j2"

#: De regel in ``nldd_field`` zoals lotc-forms hem heeft, en zoals wij hem maken.
ORIGINEEL_REGEL = "  {{ caller() }}"
ONZE_REGEL = "  {% if error %}{{ caller() | foutbedrading(id ~ '-error') }}{% else %}{{ caller() }}{% endif %}"


#: De sjabloonmap van de geinstalleerde lotc-forms.
LOTC_FORMS = Path(str(lotc_forms.__file__)).parent / "templates" / "components"


def _geinstalleerde_kopie() -> str:
    return (LOTC_FORMS / "_forms.j2").read_text()


def test_onze_kopie_wijkt_op_precies_een_regel_af() -> None:
    """Verandert lotc-forms iets anders in dit bestand, dan faalt dit - en niet de UI.

    Onze kopie ligt op de searchpath VOOR de sjablonen van de design systems en wint dus
    van het origineel. Dat is een prima manier om een bug in het thema te overbruggen en
    een slechte manier om een verbetering te MISSEN, dus wordt hij hier vergeleken.
    """
    origineel = _geinstalleerde_kopie()
    onze = ONZE_KOPIE.read_text()
    # onze kopie begint met een eigen toelichting, tot de streep
    _, streep, staart = onze.partition("---- vanaf hier de kopie ----\n\n   ")
    assert streep, "de markering die onze toelichting van de kopie scheidt is weg"
    kopie = "{# " + staart

    # alleen het NLDD-frame gaat om; rvo_field heeft dezelfde regel en blijft zoals hij is
    kop, streep_nldd, nldd = origineel.partition("{% macro nldd_field")
    assert streep_nldd, "lotc-forms heeft geen macro nldd_field meer"
    assert ORIGINEEL_REGEL in nldd, "lotc-forms rendert de besturing niet meer met een kale caller()"
    verwacht = kop + streep_nldd + nldd.replace(ORIGINEEL_REGEL, ONZE_REGEL, 1)
    assert kopie == verwacht, "lotc-forms heeft components/_forms.j2 gewijzigd; loop onze kopie na"


def test_het_origineel_heeft_de_bug_nog() -> None:
    """De reden dat wij deze kopie hebben. Weg? Dan kan de kopie ook weg."""
    origineel = _geinstalleerde_kopie()
    nldd_frame = origineel.partition("{% macro nldd_field")[2]
    assert "foutbedrading" not in nldd_frame
    # het veldsjabloon schrijft nog in error-message-ids in plaats van error-message
    tekstveld = (LOTC_FORMS / "text-input-field.html.j2").read_text()
    assert re.search(r"invalid error-message-ids=", tekstveld), (
        "lotc-forms bedraadt de fout nu zelf; haal onze kopie van components/_forms.j2 weg"
    )
