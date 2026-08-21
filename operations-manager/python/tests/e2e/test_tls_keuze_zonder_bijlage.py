"""De TLS-keuze op het echte scherm: 'aangeleverd' is niet te kiezen zonder bijlage.

De grendel uit RC-132 zit op drie plekken achter elkaar -- de provider markeert de optie,
het select-sjabloon zet ``disabled`` door, en de browser moet hem dan ook echt weigeren.
Een test op de provider of op de HTML alleen dekt de eerste twee; dit dekt de derde, op
de gerenderde pagina van ``test-project-detail`` (dat project heeft geen bijlagen).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Error as PlaywrightError
from tests.e2e.helpers.edit_modal import EditModalHelper

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
TLS_VELD = "deployments[0]/components[0]/services/publish-on-web/config/tls"


def _open_deploymentmodal(page: Page, base_url: str) -> None:
    modal = EditModalHelper(page, base_url, PROJECT)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-deployment-0", "Deployment bewerken")


def test_aangeleverd_is_uitgeschakeld_met_de_reden(app_server: str, auth_page: Page) -> None:
    _open_deploymentmodal(auth_page, app_server)

    optie = auth_page.locator(f"[name='{TLS_VELD}'] option[value='provided']").first
    assert optie.count() > 0, "de TLS-keuze biedt 'provided' helemaal niet meer aan; hij hoort te STAAN"
    assert optie.is_disabled(), "'provided' is te kiezen terwijl dit project geen bijlage heeft om aan te leveren"
    assert "Bijlagen" in (optie.text_content() or ""), "de uitgeschakelde optie zegt niet waarom hij niet kan"

    # De keuzes die wel gemaakt kunnen worden, kunnen ook echt gemaakt worden.
    for waarde in ("standard", "passthrough"):
        assert not auth_page.locator(f"[name='{TLS_VELD}'] option[value='{waarde}']").first.is_disabled()


def test_de_browser_weigert_de_uitgeschakelde_keuze(app_server: str, auth_page: Page) -> None:
    """Het einde van de keten: kiezen lukt niet, dus de onopslaanbare toestand ontstaat niet."""
    _open_deploymentmodal(auth_page, app_server)

    with pytest.raises(PlaywrightError):
        auth_page.locator(f"[name='{TLS_VELD}']").first.select_option("provided", timeout=2000)
