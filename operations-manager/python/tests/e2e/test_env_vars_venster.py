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
from tests.e2e.helpers.edit_modal import EditModalHelper
from tests.e2e.helpers.tabs import open_tab

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page

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


DOTS = "\u2022" * 8


def _open_window(auth_page: Page, app_server: str) -> None:
    auth_page.goto(f"{app_server}{COMPONENTEN_URL}")
    auth_page.wait_for_load_state("networkidle")
    open_tab(auth_page, "componenten")
    kaart = auth_page.locator(".component-card").filter(has=auth_page.get_by_text("web-app", exact=True)).first
    knop = kaart.locator("nldd-button:has-text('Omgevingsvariabelen bewerken')").first
    knop.wait_for(state="visible", timeout=10000)
    knop.click()
    auth_page.wait_for_selector(".edit-section-modal.is-open", timeout=10000)


def _open_window_with_code(auth_page: Page, app_server: str) -> Locator:
    """Open het venster, loop de step-up door, en geef de editor terug."""
    _open_window(auth_page, app_server)
    code = auth_page.locator("#edit-section-inner input[name='otp'], #edit-section-inner [name='otp'] input").first
    code.fill("000000")
    auth_page.locator("#edit-section-inner button:has-text('Bevestigen')").first.click()
    editor = auth_page.locator("#edit-section-inner .kv-editor .cm-content")
    editor.wait_for(state="visible", timeout=10000)
    return editor


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
    """De dialoog herhaalt de handeling als GET, en het venster is de enige plek met de waarden.

    Sinds RC-416 ligt er een veil over de waarden, dus de editor toont ze pas na "Toon
    waarden". Wat hier gemeten wordt is dat de HERHALING de echte regels binnenhaalt.
    """
    editor = _open_window_with_code(auth_page, app_server)
    auth_page.locator("#edit-section-inner #kv-mask-toggle-env-vars").click()

    text = editor.inner_text()
    assert "LOG_LEVEL=debug" in text
    assert "FEATURE_FLAG=on" in text


def test_the_values_are_masked_until_you_ask_for_them(app_server: str, auth_page: Page, otp_factor: object) -> None:
    """Het veil (RC-416): stippen in plaats van waarden, en de knop schakelt beide kanten op."""
    editor = _open_window_with_code(auth_page, app_server)

    gemaskeerd = editor.inner_text()
    assert f"LOG_LEVEL={DOTS}" in gemaskeerd, gemaskeerd
    assert f"FEATURE_FLAG={DOTS}" in gemaskeerd, gemaskeerd
    assert "debug" not in gemaskeerd
    assert "=on" not in gemaskeerd

    # Een screenreader krijgt de rol voorgelezen, niet de stippen en niet de waarde.
    maskers = editor.locator(".cm-kv-mask")
    assert maskers.count() == 2
    assert maskers.first.get_attribute("aria-label") == "verborgen waarde"

    # De knop is een nldd-toggle-button: het etiket en aria-pressed staan op de knop in
    # zijn shadow DOM, de aan-stand als 'selected' op het component zelf.
    knop = auth_page.locator("#edit-section-inner #kv-mask-toggle-env-vars")
    binnen = knop.locator("button")
    assert binnen.inner_text() == "Toon waarden"
    assert binnen.get_attribute("aria-pressed") == "false"
    knop.click()

    getoond = editor.inner_text()
    assert "LOG_LEVEL=debug" in getoond
    assert "FEATURE_FLAG=on" in getoond
    assert DOTS not in getoond
    assert binnen.inner_text() == "Verberg waarden"
    assert binnen.get_attribute("aria-pressed") == "true"

    knop.click()
    opnieuw = editor.inner_text()
    assert f"LOG_LEVEL={DOTS}" in opnieuw, opnieuw
    assert "debug" not in opnieuw


def test_only_looking_does_not_mark_the_modal_as_changed(app_server: str, auth_page: Page, otp_factor: object) -> None:
    """Het veil omzetten bewerkt niets, dus de modal blijft schoon (RC-416, rework).

    De change van de nldd-toggle-button bubbelt naar de luisteraar op document in
    edit_modal.js. Zonder stopPropagation vroeg sluiten na alleen "Toon waarden" om
    bevestiging van niet-opgeslagen werk dat er niet was.
    """
    # Eerst de step-up doorlopen; het invullen van de code maakt de modal wel vuil.
    _open_window_with_code(auth_page, app_server)
    auth_page.evaluate("closeEditModal()")

    # Tweede opening valt binnen het verse step-up-venster: geen code, dus modal schoon.
    _open_window(auth_page, app_server)
    editor = auth_page.locator("#edit-section-inner .kv-editor .cm-content")
    editor.wait_for(state="visible", timeout=10000)
    assert auth_page.evaluate("window.isEditDirty") is False

    auth_page.locator("#edit-section-inner #kv-mask-toggle-env-vars").click()
    assert auth_page.locator("#edit-section-inner .cm-kv-mask").count() == 0
    assert auth_page.evaluate("window.isEditDirty") is False


def test_the_cursor_line_shows_its_own_value(app_server: str, auth_page: Page, otp_factor: object) -> None:
    """Een waarde bewerken zonder de rest te zien: alleen de regel met de cursor gaat open."""
    editor = _open_window_with_code(auth_page, app_server)

    editor.locator(".cm-line").nth(1).click()
    text = editor.inner_text()
    assert "FEATURE_FLAG=on" in text, text
    assert f"LOG_LEVEL={DOTS}" in text, text


def test_editing_one_masked_line_keeps_the_other_values(app_server: str, auth_page: Page, otp_factor: object) -> None:
    """Het veil raakt het document niet: wat opgeslagen wordt houdt de ongemoeide waarden.

    Het meetpunt is de verborgen textarea, want dat is wat het formulier verstuurt.
    """
    editor = _open_window_with_code(auth_page, app_server)

    regel = editor.locator(".cm-line").first
    regel.click()
    auth_page.keyboard.press("Home")
    auth_page.keyboard.press("Shift+End")
    auth_page.keyboard.type("LOG_LEVEL=trace")

    verzonden = auth_page.locator("#edit-section-inner textarea[name='vars']").input_value()
    assert "LOG_LEVEL=trace" in verzonden, verzonden
    assert "FEATURE_FLAG=on" in verzonden, verzonden
    assert DOTS not in verzonden, verzonden


@pytest.mark.parametrize("weergave", ["light", "dark"])
def test_the_dots_stay_legible_in_both_schemes(
    app_server: str, auth_page: Page, otp_factor: object, weergave: str
) -> None:
    """De stippen krijgen een themategen, dus ze mogen in geen van beide weergaven wegvallen."""
    auth_page.emulate_media(color_scheme=weergave)
    try:
        editor = _open_window_with_code(auth_page, app_server)
        masker = editor.locator(".cm-kv-mask").first
        masker.wait_for(state="visible", timeout=10000)
        kleuren = masker.evaluate(
            "el => { const s = getComputedStyle(el);"
            " return [s.color, getComputedStyle(el.closest('.cm-editor')).backgroundColor]; }"
        )
        voorgrond, achtergrond = kleuren
        assert voorgrond != achtergrond, f"{weergave}: stippen in de achtergrondkleur"
        assert "rgba(0, 0, 0, 0)" not in voorgrond, f"{weergave}: stippen zonder kleur"
    finally:
        auth_page.emulate_media(color_scheme="light")


def test_comment_and_empty_lines_are_never_masked(app_server: str, auth_page: Page, otp_factor: object) -> None:
    """Commentaarregels en lege regels blijven leesbaar, ook als ze een `=` bevatten."""
    editor = _open_window_with_code(auth_page, app_server)

    editor.click()
    auth_page.keyboard.press("ControlOrMeta+a")
    auth_page.keyboard.type("# KEY=een-geheim-dat-niet-bestaat\n\nLOG_LEVEL=debug\nFEATURE_FLAG=on")
    # Cursor op een vierde regel: anders toont de uitzondering voor de cursorregel toevallig
    # dezelfde regel die hier getest wordt (comment of LOG_LEVEL), en meet de toets niets.
    editor.locator(".cm-line").nth(3).click()

    tekst = editor.inner_text()
    assert "# KEY=een-geheim-dat-niet-bestaat" in tekst, tekst
    assert f"LOG_LEVEL={DOTS}" in tekst, tekst
    assert "debug" not in tekst, tekst
    assert editor.locator(".cm-kv-mask").count() == 1


def test_the_aliases_field_is_not_masked(app_server: str, auth_page: Page) -> None:
    """Aliassen zijn namen van platform-variabelen, geen geheimen, dus geen veil."""
    modal = EditModalHelper(auth_page, app_server, PROJECT)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-components", "Components beheren")

    veld = auth_page.locator(".kv-editor:has(textarea[name='components[0]/aliases'])")
    veld.wait_for(state="visible", timeout=10000)
    assert veld.get_attribute("data-mask-values") is None
    assert veld.locator(".cm-kv-mask").count() == 0


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
