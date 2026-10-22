"""De OTAP-markering van een deployment in het portaal (RC-330, planonderdeel 4).

Gemeten door de browser en de echte routes: het keuzeveld op de deploymentmodal, en dat de
projectinstellingen er GEEN keuzeveld voor hebben. De markering hoort bij een deployment,
en op de projectpagina staat alleen de afgeleide hoogste markering.

Het SCHRIJVEN van de deploymentmarkering is hier niet te meten: die opslag doet
``process_project`` en zonder takenservice antwoordt die route met een 503. De schrijfweg
zelf staat in ``tests/forms/test_otap_markering_schrijfweg.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.edit_modal import EditModalHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT_NAME = "test-project-detail"
DEPLOYMENT_INDEX = 0
DEPLOYMENT_VELD = f"deployments[{DEPLOYMENT_INDEX}]/environment"


def test_de_deploymentmodal_biedt_de_standaard_als_eerste_keuze(app_server: str, auth_page: Page) -> None:
    modal = EditModalHelper(auth_page, app_server, PROJECT_NAME)
    modal.open_detail_page()
    modal.open_edit_modal(f"modal-edit-deployment-{DEPLOYMENT_INDEX}", "Deployment bewerken")

    keuze = auth_page.locator(f"[name='{DEPLOYMENT_VELD}']").first
    assert keuze.count() > 0, "de deploymentmodal biedt geen keuzeveld voor de omgeving"
    eerste = keuze.locator("option").first
    assert eerste.get_attribute("value") == "", "de eerste keuze hoort de lege waarde (de standaard) te zijn"
    assert "Ontwikkeling" in (eerste.text_content() or "")
    assert keuze.input_value() == "", "deze deployment heeft geen markering, dus het veld staat op de standaard"


def test_de_projectinstellingen_kennen_de_markering_niet(app_server: str, auth_page: Page) -> None:
    """Op projectniveau is de markering afgeleid, dus niet te bewerken."""
    modal = EditModalHelper(auth_page, app_server, PROJECT_NAME)
    modal.open_detail_page()

    # Op het attribuut: een nldd-tag zet zijn label in ``text`` en tekent het in een
    # shadow root, dus tekst zoeken in de pagina wordt vacuut groen.
    assert auth_page.locator("nldd-tag[text='Ontwikkeling']").count() > 0, (
        "de projectpagina toont de afgeleide hoogste markering niet"
    )

    modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")
    assert auth_page.locator("[name='environment']").count() == 0
