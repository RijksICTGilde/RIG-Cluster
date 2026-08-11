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
from typing import TYPE_CHECKING

import pytest
from opi.core.templates_lotc import templates_lotc
from opi.services.catalog.invite.visualizers import INVITE_REALM_ROLE_ITEM
from opi.services.catalog.keycloak.visualizers import (
    KEYCLOAK_CLIENT_REDIRECT_URI,
    KEYCLOAK_REDIRECT_URI_ITEM,
)

if TYPE_CHECKING:
    from opi.forms.visualizers.visualizer import EditableVisualizer

WORTEL = Path(__file__).resolve().parent.parent

MERK = """ :attrs="{'data-no-optional-badge': '1'}\""""


def _render(extra: str) -> str:
    bron = f'<c-text-input-field id="v" name="v" label="URI"{extra}/>'
    return templates_lotc.env.from_string(bron).render()


def test_zonder_merk_blijft_optioneel_staan() -> None:
    """De conventie blijft de standaard; dit is een uitzondering en geen omzetting."""
    assert " optional " in _render("")


def test_het_merk_haalt_optioneel_weg() -> None:
    assert " optional " not in _render(MERK)


def test_het_merk_maakt_het_veld_niet_verplicht() -> None:
    """Het verschil met de omweg: de HTML gaat niets anders beweren."""
    html = _render(MERK)
    assert " required" not in html
    # en het merk zelf komt gewoon op de besturing terecht
    assert 'data-no-optional-badge="1"' in html


@pytest.mark.parametrize(
    ("naam", "visualizer"),
    [
        ("extra redirect-URI", KEYCLOAK_REDIRECT_URI_ITEM),
        ("client redirect-URI", KEYCLOAK_CLIENT_REDIRECT_URI),
        # Dezelfde vorm, gevonden bij het nalopen van de formulieren: het enige veld van
        # een regel die je toevoegt, en bovendien een kiezer met een lege stand die
        # "Geen rol toekennen" heet.
        ("realm-rol van een uitnodiging", INVITE_REALM_ROLE_ITEM),
    ],
)
def test_de_velden_zonder_zinnige_badge_dragen_het_merk(naam: str, visualizer: EditableVisualizer) -> None:
    del naam
    assert (visualizer.attributes or {}).get("data-no-optional-badge") == "1"
    # het merk vervangt de omweg en mag het veld dus niet verplicht maken
    assert visualizer.editable.required is False


def test_elk_veld_heeft_zijn_eigen_bundel() -> None:
    """Een gedeelde dict zou verderop bij EEN veld aangevuld worden en bij allemaal gelden."""
    assert KEYCLOAK_REDIRECT_URI_ITEM.attributes is not KEYCLOAK_CLIENT_REDIRECT_URI.attributes


def test_de_deploymentkiezer_gebruikt_het_merk_en_geen_required() -> None:
    """De kiezer stond even op required="true" om het label kwijt te raken."""
    bron = (WORTEL / "opi" / "templates_lotc" / "bg" / "project-tabs.html.j2").read_text()
    kiezer = bron.partition("global-deployment-selector")[2].partition("</c-select-field>")[0]
    assert kiezer, "de deploymentkiezer staat niet meer in project-tabs.html.j2"
    assert 'required="true"' not in kiezer
    assert "data-no-optional-badge" in bron.partition("global-deployment-selector")[0][-800:]
