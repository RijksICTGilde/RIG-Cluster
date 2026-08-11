"""Waar "Optioneel" niets betekent, staat het er niet.

lotc-forms zet ``optional`` op elk NLDD-veld dat niet verplicht is - de rijksconventie is
"markeer optioneel, niet verplicht". Voor een invoerveld klopt dat. Voor een KIEZER waar
altijd iets geselecteerd staat betekent het niets, en bij het enige veld van een regel
die je zelf toevoegde leest "URI Optioneel" als ruis.

De omweg was zulke velden ``required`` noemen: het label verdwijnt, maar de HTML zegt dan
dat er iets ingevuld MOET worden en dat is een andere onwaarheid (en een echte:
formuliervalidatie leest dat attribuut). ``data-no-optional-badge`` zegt alleen wat het
is. Onze kopie van ``components/_forms.j2`` leest hem; het derde standje hoort in het
thema en staat als verzoek in request_for_components.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from opi.core.templates_lotc import templates_lotc
from opi.services.catalog.keycloak.visualizers import (
    KEYCLOAK_CLIENT_REDIRECT_URI,
    KEYCLOAK_REDIRECT_URI_ITEM,
)

WORTEL = Path(__file__).resolve().parent.parent


def _render(extra: str) -> str:
    bron = f'<c-text-input-field id="v" name="v" label="URI"{extra}/>'
    return templates_lotc.env.from_string(bron).render()


def test_zonder_merk_blijft_optioneel_staan() -> None:
    """De conventie blijft de standaard; dit is een uitzondering en geen omzetting."""
    assert " optional " in _render("")


def test_het_merk_haalt_optioneel_weg() -> None:
    assert " optional " not in _render(""" :attrs="{'data-no-optional-badge': '1'}\"""")


def test_het_merk_maakt_het_veld_niet_verplicht() -> None:
    """Het verschil met de omweg: de HTML gaat niets anders beweren."""
    html = _render(""" :attrs="{'data-no-optional-badge': '1'}\"""")
    assert " required" not in html


@pytest.mark.parametrize(
    ("naam", "visualizer"),
    [("extra redirect-URI", KEYCLOAK_REDIRECT_URI_ITEM), ("client redirect-URI", KEYCLOAK_CLIENT_REDIRECT_URI)],
)
def test_de_gemelde_velden_dragen_het_merk(naam: str, visualizer: object) -> None:
    """De twee "URI Optioneel"-velden uit de melding."""
    del naam
    assert getattr(visualizer, "attributes", {}).get("data-no-optional-badge") == "1"


def test_de_deploymentkiezer_gebruikt_het_merk_en_geen_required() -> None:
    """De kiezer stond even op required="true" om het label kwijt te raken."""
    bron = (WORTEL / "opi" / "templates_lotc" / "bg" / "_deployment-selector.html.j2").read_text()
    # zonder de toelichting: die vertelt juist waarom required="true" er niet meer staat
    markup = bron.partition("#}")[2]
    assert "data-no-optional-badge" in markup
    assert 'required="true"' not in markup
