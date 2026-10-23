"""Een dienst MET een configuratiestap uitvinken via de dienstenmodal.

De bestaande modaltoetsen vinken ``authorization-wall`` of ``keycloak`` aan en uit,
diensten zonder eigen configuratiestap. Een dienst met zo'n stap post in die stap zijn
eigen dienstenlijst, en daarmee loopt de deselectie langs een tweede lijst: blijft de
naam daar staan, dan zet de vereniging in ``merge_service_lists`` hem terug, blijft de
configuratiestap zichtbaar, en is het eindresultaat gelijk aan wat er stond. Geen enkele
toets legde dat pad af, in de browser noch in het formulierharnas.

De tweede toets hieronder is waar het echt op stuk ging: zolang er NOG een dienst
aanstaat landt het uitvinken wel. Staat de uitgevinkte dienst alleen, dan stuurt het
formulier helemaal geen dienstenlijst meer, en afwezig betekende in de verwerker
"ongewijzigd" (de kaartenselectie viel niet in de tak van de vinkjeslijst, die afwezig
wel als lege selectie leest).

``minio-storage`` is de dienst hier omdat hij die vorm heeft (een eigen
``config_section_id`` plus een modal-flow) en op elk cluster beschikbaar is.
``seaweedfs-storage`` heeft dezelfde vorm maar bestaat alleen op ``sandboxed-local``, dus
die kant meet ``tests/e2e/test_sandbox_seaweedfs_storage.py``.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

import pytest
from tests.e2e.helpers.edit_modal import EditModalHelper

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT_NAME = "test-project-detail"
DIENST = "minio-storage"


def _opgeslagen() -> dict[str, Any]:
    from opi.services.project_service import get_project_service

    project = get_project_service().get_project(PROJECT_NAME)
    return project.data


def _dienstnamen() -> list[str]:
    from opi.services.services import service_entry_name

    return [service_entry_name(entry) for entry in _opgeslagen().get("services") or []]


def _zet_dienst_in_het_project(alleen: bool) -> Iterator[None]:
    """Zet de dienst met config in het gedeelde fixtureproject, en haal hem daarna weg.

    Het project en de store zijn sessiebreed: wat deze toets achterlaat ziet elke latere
    toets die dit project opent. Aanzetten gaat buiten de wizard om, want het subject is
    het UITvinken; dan hoeft de toets niet eerst een hele flow door om op zijn beginstand
    te komen.
    """
    from opi.services.project_service import get_project_service

    service = get_project_service()
    stored = service.get_project(PROJECT_NAME)
    origineel, filename = deepcopy(stored.data), stored.filename

    vermelding = {"name": DIENST, "config": {"enable-versioning": True}}
    met_dienst = deepcopy(stored.data)
    if alleen:
        met_dienst["services"] = [vermelding]
        for component in met_dienst["components"]:
            component["services"] = []
    else:
        met_dienst["services"].append(vermelding)
    met_dienst["components"][0]["services"] = [*(met_dienst["components"][0].get("services") or []), DIENST]
    service.load_project_from_data(met_dienst, filename)
    try:
        yield
    finally:
        service.load_project_from_data(deepcopy(origineel), filename)


@pytest.fixture
def _project_met_de_dienst(app_server: str) -> Iterator[None]:
    yield from _zet_dienst_in_het_project(alleen=False)


@pytest.fixture
def _project_met_alleen_de_dienst(app_server: str) -> Iterator[None]:
    yield from _zet_dienst_in_het_project(alleen=True)


def _vink_uit_en_sla_op(modal: EditModalHelper) -> None:
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-services", "Services beheren")
    assert modal.is_service_selected(DIENST), f"'{DIENST}' staat niet aan op de kaart"

    modal.toggle_service(DIENST)
    assert not modal.is_service_selected(DIENST), "de klik kwam niet aan op de kaart"
    modal.submit_step()
    modal.advance_to_review()

    # Deze flow start een uitrol, dus de confirm wisselt een voortgangsscherm in plaats
    # van tot rust te komen: wacht op het opslagverzoek zelf.
    confirm = modal.page.locator(
        ".wizard-review button[type='submit'], button:has-text('Bevestigen'), button:has-text('Opslaan')"
    ).first
    with modal.page.expect_response(
        lambda response: response.request.method == "POST" and response.url.split("?")[0].endswith("/confirm")
    ):
        confirm.click()


@pytest.mark.usefixtures("_project_met_de_dienst")
def test_uitvinken_haalt_de_dienst_uit_het_projectbestand(app_server: str, auth_page: Page) -> None:
    assert DIENST in _dienstnamen(), "de fixture heeft de dienst niet aangezet"

    _vink_uit_en_sla_op(EditModalHelper(auth_page, app_server, PROJECT_NAME))

    assert DIENST not in _dienstnamen(), f"de dienst staat na het uitvinken nog in het project: {_dienstnamen()}"
    component_diensten = _opgeslagen()["components"][0].get("services") or []
    assert DIENST not in component_diensten, f"de binding op de component staat nog: {component_diensten}"


@pytest.mark.usefixtures("_project_met_alleen_de_dienst")
def test_de_laatst_overgebleven_dienst_uitvinken_landt_ook(app_server: str, auth_page: Page) -> None:
    """Zonder tweede dienst stuurt het formulier geen dienstenlijst, en dat is leeg."""
    assert _dienstnamen() == [DIENST], f"de fixture laat niet alleen deze dienst over: {_dienstnamen()}"

    _vink_uit_en_sla_op(EditModalHelper(auth_page, app_server, PROJECT_NAME))

    assert _dienstnamen() == [], f"het project houdt diensten over: {_dienstnamen()}"
