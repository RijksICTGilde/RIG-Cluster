"""Elke bevestiging is een dialoog van het thema, ook vanuit een open bewerkmodal.

Er mag geen systeemvenster meer komen. Dat is te meten: ``page.on("dialog", ...)`` vangt
elke ``confirm()`` en ``alert()`` van de browser, en elke toets hier faalt als die lijst
niet leeg is.

De tweede vraag van RC-395 was of een dialoog BOVEN de gedeelde bewerkmodal kan staan. Dat
kan, en de meting is de klik zelf: ``<nldd-modal-dialog>`` draait op een echte ``<dialog>``
met ``showModal()`` en komt daarmee in de toplaag van de browser, terwijl de bewerkmodal een
gewone div met een z-index is. Playwright weigert een klik op een element dat bedekt is, dus
een geslaagde klik op Annuleren terwijl de modal open staat IS het bewijs.

De bevestiging van een gebruikersactie staat in ``test_user_admin.py``, bij de pagina die
hem draagt.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.tabs import open_tab

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"

#: Het token dat tests/e2e/fixtures/projects/test-project-detail.yaml meelevert, bij het lid
#: dat ook de ingelogde gebruiker is. Die is beheerder, dus hij mag intrekken.
TOKEN = "cli"
INTREKKEN = f"/projects/{PROJECT}/leden/test@example.com/tokens/{TOKEN}/intrekken"


def _vang_systeemvensters(page: Page) -> list[str]:
    vensters: list[str] = []
    page.on("dialog", lambda d: (vensters.append(d.message), d.dismiss()))
    return vensters


def test_een_token_intrekken_vraagt_in_de_dialoog(app_server: str, auth_page: Page) -> None:
    """De vraag noemt het token; annuleren laat de POST niet lopen en bevestigen wel.

    Wat de POST antwoordt is van de step-up en niet van deze taak. Dat het htmx-verzoek na
    een bevestiging vertrekt is hier de meting: htmx houdt het vast tot ``issueRequest()``.
    """
    vensters = _vang_systeemvensters(auth_page)
    posts: list[str] = []
    auth_page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and INTREKKEN in r.url else None)

    auth_page.goto(f"{app_server}/projects/{PROJECT}/details")
    open_tab(auth_page, "team")

    auth_page.locator(f"form[action='{INTREKKEN}'] button[type='submit']").first.click()

    dialoog = auth_page.locator("#bevestiging-dialoog")
    dialoog.wait_for(state="visible", timeout=5000)
    assert TOKEN in (dialoog.get_attribute("text") or ""), dialoog.get_attribute("text")

    auth_page.locator("#bevestiging-annuleren").click()
    dialoog.wait_for(state="hidden", timeout=5000)
    assert not posts, f"annuleren stuurde de POST alsnog: {posts}"

    # En bevestigen geeft het htmx-verzoek wel vrij. Wat de POST antwoordt is van de
    # step-up en niet van deze taak; dat hij vertrekt is hier de meting.
    auth_page.locator(f"form[action='{INTREKKEN}'] button[type='submit']").first.click()
    dialoog.wait_for(state="visible", timeout=5000)
    with auth_page.expect_request(lambda r: r.method == "POST" and INTREKKEN in r.url, timeout=5000):
        auth_page.locator("#bevestiging-doorgaan").click()

    assert not vensters, f"er kwam een systeemvenster: {vensters}"


def test_niet_opgeslagen_wijzigingen_vragen_boven_de_bewerkmodal(app_server: str, auth_page: Page) -> None:
    """Een klik naast een gewijzigde bewerkmodal vraagt, in een dialoog erboven."""
    vensters = _vang_systeemvensters(auth_page)

    auth_page.goto(f"{app_server}/projects/{PROJECT}/details")
    auth_page.evaluate("openEditModal('identity', 'Projectgegevens', {})")
    auth_page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=10000)
    auth_page.evaluate("window.isEditDirty = true")

    auth_page.evaluate("handleEditBackdropClick()")
    dialoog = auth_page.locator("#bevestiging-dialoog")
    dialoog.wait_for(state="visible", timeout=5000)

    # Annuleren houdt de bewerking open. De klik slaagt alleen als de dialoog boven de
    # modal staat; dat is hier de meting.
    auth_page.locator("#bevestiging-annuleren").click()
    dialoog.wait_for(state="hidden", timeout=5000)
    assert auth_page.locator("#edit-section-modal.is-open").count() == 1, "annuleren sloot de bewerking"

    auth_page.evaluate("handleEditBackdropClick()")
    dialoog.wait_for(state="visible", timeout=5000)
    auth_page.locator("#bevestiging-doorgaan").click()
    auth_page.locator("#edit-section-modal.is-open").wait_for(state="hidden", timeout=5000)
    assert not vensters, f"er kwam een systeemvenster: {vensters}"


def test_escape_op_de_vraag_laat_de_bewerkmodal_staan(app_server: str, auth_page: Page) -> None:
    """Escape sluit alleen de vraag. Het niet-opgeslagen werk blijft staan.

    De keydown komt uit de toplaag en bubbelt door naar document, waar edit_modal.js de
    bewerkmodal sluit. Escape gaf daarmee precies weg wat de vraag moet beschermen.
    """
    vensters = _vang_systeemvensters(auth_page)

    auth_page.goto(f"{app_server}/projects/{PROJECT}/details")
    auth_page.evaluate("openEditModal('identity', 'Projectgegevens', {})")
    auth_page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=10000)
    auth_page.evaluate("window.isEditDirty = true")

    auth_page.evaluate("handleEditBackdropClick()")
    dialoog = auth_page.locator("#bevestiging-dialoog")
    dialoog.wait_for(state="visible", timeout=5000)

    auth_page.keyboard.press("Escape")
    dialoog.wait_for(state="hidden", timeout=5000)
    assert auth_page.locator("#edit-section-modal.is-open").count() == 1, "Escape sloot de bewerking"
    assert auth_page.evaluate("window.isEditDirty") is True, "Escape gooide de wijzigingen weg"

    # En de vraag is daarna gewoon opnieuw te stellen.
    auth_page.evaluate("handleEditBackdropClick()")
    dialoog.wait_for(state="visible", timeout=5000)
    auth_page.locator("#bevestiging-annuleren").click()
    dialoog.wait_for(state="hidden", timeout=5000)
    assert not vensters, f"er kwam een systeemvenster: {vensters}"


def test_vraag_wegklikken_laat_escape_daarna_werken(app_server: str, auth_page: Page) -> None:
    """Light dismiss van de component ruimt de vraag ook op.

    ``<nldd-modal-dialog>`` sluit zelf op een klik naast de dialoog, buiten de twee knoppen
    om. Bleef de Escape-afhandeling van de vraag dan hangen, dan at die de eerstvolgende
    Escape op en ging de bewerkmodal pas op de tweede dicht.
    """
    vensters = _vang_systeemvensters(auth_page)

    auth_page.goto(f"{app_server}/projects/{PROJECT}/details")
    auth_page.evaluate("openEditModal('identity', 'Projectgegevens', {})")
    auth_page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=10000)
    auth_page.evaluate("window.isEditDirty = true")

    auth_page.evaluate("handleEditBackdropClick()")
    dialoog = auth_page.locator("#bevestiging-dialoog")
    dialoog.wait_for(state="visible", timeout=5000)

    # Naast de dialoogdoos, die op deze breedte ruim voorbij x=400 begint.
    auth_page.mouse.click(40, 40)
    dialoog.wait_for(state="hidden", timeout=5000)
    assert auth_page.locator("#edit-section-modal.is-open").count() == 1, "wegklikken sloot de bewerking"

    auth_page.keyboard.press("Escape")
    auth_page.locator("#edit-section-modal.is-open").wait_for(state="hidden", timeout=5000)
    assert not vensters, f"er kwam een systeemvenster: {vensters}"


def test_een_swap_onder_de_vraag_laat_escape_door(app_server: str, auth_page: Page) -> None:
    """Gaat de dialoog uit de pagina, dan mag Escape door naar wat er wel staat.

    Een htmx-swap kan het dialoogelement vervangen terwijl de vraag open staat. De
    Escape-afhandeling hoort dan niet bij een venster dat er nog is, en at anders de
    eerstvolgende Escape op.
    """
    vensters = _vang_systeemvensters(auth_page)

    auth_page.goto(f"{app_server}/projects/{PROJECT}/details")
    auth_page.evaluate("openEditModal('identity', 'Projectgegevens', {})")
    auth_page.locator("#edit-section-modal.is-open").wait_for(state="visible", timeout=10000)
    auth_page.evaluate("window.isEditDirty = true")

    auth_page.evaluate("handleEditBackdropClick()")
    auth_page.locator("#bevestiging-dialoog").wait_for(state="visible", timeout=5000)

    # Wat een swap met het dialoogelement doet: het verdwijnt uit de pagina.
    auth_page.evaluate("document.getElementById('bevestiging-dialoog').remove()")

    auth_page.keyboard.press("Escape")
    auth_page.locator("#edit-section-modal.is-open").wait_for(state="hidden", timeout=5000)
    assert not vensters, f"er kwam een systeemvenster: {vensters}"
