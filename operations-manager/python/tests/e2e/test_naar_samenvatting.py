"""De sprong "Naar samenvatting" in de paginawizard.

De knop staat op elke stap (niet pas nadat alle stappen ooit afgerond zijn), maar de
sprong valideert eerst de HELE flow: wie een stap overslaat waar nog een verplichte
waarde mist, landt op precies die stap met de fout gemarkeerd, en niet op een
samenvatting met gaten.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.lifecycle import REVIEW_SUBMIT_SELECTOR
from tests.e2e.helpers.wizard import WizardHelper, unique_project_name

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

RUNNABLE_IMAGE = "ghcr.io/minbzk/base-images/e2e-allservices:latest"


def _klik_naar_samenvatting(page: Page) -> None:
    knop = page.locator("button:has-text('Naar samenvatting')").first
    knop.wait_for(state="visible", timeout=10000)
    knop.click()


def _naar_de_componentenstap(wizard: WizardHelper, page: Page) -> None:
    """Vul de stappen tot en met team en land op de (lege) componentenstap."""
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name(), description="sprong naar samenvatting")
    wizard.click_next()  # identity -> services
    wizard.fill_services([])
    wizard.click_next()  # services -> team
    wizard.fill_team(email="test@example.com")
    wizard.click_next()  # team -> componenten
    page.wait_for_load_state("networkidle")


def test_sprong_stuit_op_een_gemiste_verplichte_waarde(app_server: str, auth_page: Page) -> None:
    """Terug van de componentenstap en dan springen: de lege component houdt je tegen.

    De componentenstap is bezocht (zijn lege regel staat in de wizardstand) maar het
    verplichte imageveld is leeg. De sprong vanaf de teamstap moet daarop stranden en
    de gebruiker op de componentenstap neerzetten, met de fout gemarkeerd.
    """
    wizard = WizardHelper(auth_page, app_server)
    _naar_de_componentenstap(wizard, auth_page)

    wizard.click_previous()  # terug naar team; de lege componentregel is nu bewaard
    _klik_naar_samenvatting(auth_page)

    wizard.wait_for_step("Componenten")
    auth_page.locator('[aria-invalid="true"]').first.wait_for(state="visible", timeout=10000)


def test_sprong_landt_op_de_samenvatting_als_alles_klopt(app_server: str, auth_page: Page) -> None:
    """Met alle verplichte waardes gevuld slaat de sprong de resterende stappen over."""
    wizard = WizardHelper(auth_page, app_server)
    _naar_de_componentenstap(wizard, auth_page)
    wizard.fill_component(name="web", image=RUNNABLE_IMAGE)

    # Een keer langs de deploymentstap zodat ook diens verplichte naam gevuld is,
    # dan terug en vanaf de componentenstap springen.
    wizard.click_next()
    auth_page.wait_for_load_state("networkidle")
    wizard.click_previous()

    _klik_naar_samenvatting(auth_page)
    auth_page.locator(REVIEW_SUBMIT_SELECTOR).first.wait_for(state="visible", timeout=10000)
