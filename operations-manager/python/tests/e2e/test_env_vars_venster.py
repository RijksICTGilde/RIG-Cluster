"""Het omgevingsvariabelen-venster achter een verse step-up (RC-388).

Waarom in een browser: hier moeten vier dingen op elkaar passen die geen van alle uit de
HTML blijken - het potloodje op de componentkaart opent de modal via ``openServiceModal``,
de route wisselt zichzelf in voor de step-up-dialoog zonder verse stempel, de herhaling
na de code is een GET, en pas daarna toont het venster de ECHTE waarden. De Kaart zelf
(alleen namen) dekt ``tests/e2e/test_componentkaart_uitklap.py``.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.services.user_otp_service import OtpStatus, OtpVerification
from tests.e2e.helpers.tabs import open_tab

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
COMPONENTEN_URL = f"/projects/{PROJECT}/componenten"


@pytest.fixture
def otp_factor() -> object:
    """Deze gebruiker heeft een factor, en elke code is goed.

    De OTP-dienst zelf staat in ``tests/test_user_otp_service.py``; hier gaat het om de
    dialoog, de herhaling en wat het venster daarna toont.
    """
    dienst = MagicMock(
        status=AsyncMock(return_value=OtpStatus(registered=True)),
        verify=AsyncMock(return_value=OtpVerification.OK),
    )
    with (
        patch("opi.web.step_up_dialog.UserOtpService", return_value=dienst),
        patch("opi.web.router_step_up.UserOtpService", return_value=dienst),
    ):
        yield


def _open_window(auth_page: Page, app_server: str) -> None:
    auth_page.goto(f"{app_server}{COMPONENTEN_URL}")
    auth_page.wait_for_load_state("networkidle")
    open_tab(auth_page, "componenten")
    kaart = auth_page.locator(".component-card").filter(has=auth_page.get_by_text("web-app", exact=True)).first
    knop = kaart.locator("nldd-button:has-text('Omgevingsvariabelen bewerken')").first
    knop.wait_for(state="visible", timeout=10000)
    knop.click()
    auth_page.wait_for_selector(".edit-section-modal.is-open", timeout=10000)


def test_opening_asks_for_a_code_first_and_shows_no_value(app_server: str, auth_page: Page, otp_factor: object) -> None:
    """Zonder verse step-up komt de OTP-dialoog in de modal, met de GET-herhaling erin."""
    _open_window(auth_page, app_server)

    dialoog = auth_page.locator("#edit-section-inner")
    dialoog.locator("[data-step-up]").wait_for(state="visible", timeout=10000)
    assert "omgevingsvariabelen" in dialoog.inner_text().lower()
    assert dialoog.locator('input[name="otp"], [name="otp"] input').count() >= 1
    assert dialoog.locator('input[name="vervolg_methode"]').input_value() == "GET"

    # De waarden van het fixtureproject horen hier nog niet te staan. Naam-WAARDE-paren,
    # niet "debug" alleen: dat woord komt ook zonder context elders in de pagina voor.
    content = auth_page.content()
    assert "LOG_LEVEL=debug" not in content
    assert "FEATURE_FLAG=on" not in content
    # De terugval waar deze toets tegen bewaakt: de oude kaart zette de waarde in het
    # value-attribuut van een geheimveld. Een paar staat daar nooit aaneen, dus alleen
    # dit attribuut maakt "waarde in de HTML" echt meetbaar.
    assert 'value="debug"' not in content


def test_after_the_code_the_window_shows_the_real_values(app_server: str, auth_page: Page, otp_factor: object) -> None:
    """De dialoog herhaalt de handeling als GET, en het venster is de enige plek met de waarden."""
    _open_window(auth_page, app_server)

    code = auth_page.locator("#edit-section-inner input[name='otp'], #edit-section-inner [name='otp'] input").first
    code.fill("000000")
    auth_page.locator("#edit-section-inner button:has-text('Bevestigen')").first.click()

    # De herhaling haalt het venster met een GET binnen; de editor toont de echte regels.
    editor = auth_page.locator("#edit-section-inner .kv-editor .cm-content")
    editor.wait_for(state="visible", timeout=10000)
    text = editor.inner_text()
    assert "LOG_LEVEL=debug" in text
    assert "FEATURE_FLAG=on" in text


@pytest.fixture
def task_started() -> object:
    """De lokale testserver heeft geen taakdienst; vang alleen de start op.

    Wat de route aan de taakdienst vraagt (create_project met de al-opgeslagen stand) is
    hier het meetpunt; de verwerking zelf staat in de sandbox-meting.
    """
    from fastapi.responses import HTMLResponse

    started = AsyncMock(return_value=HTMLResponse("<div class='edit-progress-view'>verwerkt</div>"))
    with patch("opi.web.router_env_vars.create_task_and_render_progress", started):
        yield started


def test_saving_writes_encrypted_and_leaves_nothing_on_the_card(
    app_server: str, auth_page: Page, otp_factor: object, task_started: object
) -> None:
    """Opslaan toetst de step-up opnieuw; het bestand krijgt een AGE-blok, de kaart geen naam en geen waarde."""
    _open_window(auth_page, app_server)
    code = auth_page.locator("#edit-section-inner input[name='otp'], #edit-section-inner [name='otp'] input").first
    code.fill("000000")
    auth_page.locator("#edit-section-inner button:has-text('Bevestigen')").first.click()
    editor = auth_page.locator("#edit-section-inner .kv-editor .cm-content")
    editor.wait_for(state="visible", timeout=10000)

    original = editor.inner_text()
    try:
        editor.click()
        auth_page.keyboard.press("ControlOrMeta+a")
        auth_page.keyboard.type("LOG_LEVEL=info\nFEATURE_FLAG=on\nNIEUW=echt-geheim")
        auth_page.locator("#edit-section-inner button:has-text('Opslaan')").first.click()

        # De step-up van zojuist is nog vers, dus geen tweede vraag: de taak start.
        auth_page.wait_for_selector("#edit-section-inner .edit-progress-view", timeout=10000)
        assert task_started.await_count == 1
        assert task_started.await_args.kwargs["payload"]["project_file_already_saved"] is True

        from opi.services.project_store import get_project_store

        stored = (get_project_store().get(PROJECT).data or {})["components"][0]["user-env-vars"]
        assert "echt-geheim" not in str(stored)
        assert "BEGIN AGE ENCRYPTED FILE" in str(stored)
        assert "echt-geheim" not in auth_page.content()
    finally:
        # De fixture is van de hele sessie: zet terug wat er stond, via dezelfde opslagweg.
        from opi.services.project_store import get_project_store

        data = copy.deepcopy(get_project_store().get(PROJECT).data or {})
        data["components"][0]["user-env-vars"] = "LOG_LEVEL=debug\nFEATURE_FLAG=on\n"
        store = get_project_store()
        store._refresh_cache(PROJECT, data, f"{PROJECT}.yaml")

        assert "LOG_LEVEL" not in _names_on_page(auth_page, app_server)


def _names_on_page(auth_page: Page, app_server: str) -> str:
    auth_page.goto(f"{app_server}{COMPONENTEN_URL}")
    auth_page.wait_for_load_state("networkidle")
    open_tab(auth_page, "componenten")
    return auth_page.content()
