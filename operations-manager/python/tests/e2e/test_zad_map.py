"""De kaart in de browser: het tabblad, het paneel, het uitklappen en /platform.

Dit is de enige meting die het ELEMENT echt draait, onder de CSP die de app zelf stuurt.
Een bundel die daar niet langs komt rendert niets en geeft geen foutmelding op het scherm,
dus een groene unittest op de graaf zegt hier niets over.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

PROJECT = "test-project-detail"
#: De kaart tekent zichzelf na een fetch en een layoutronde; dit is de uitersttijd.
TIMEOUT = 15_000


def _map(page: Page):
    """De shadow root van het element; Playwright kijkt er vanzelf doorheen."""
    return page.locator("zad-map")


def _open(page: Page, base: str, path: str) -> None:
    page.goto(f"{base}{path}")
    expect(_map(page).locator(".svelte-flow__node").first).to_be_visible(timeout=TIMEOUT)


def test_tab_shows_nodes(auth_page: Page, app_server: str) -> None:
    _open(auth_page, app_server, f"/projects/{PROJECT}/kaart")
    nodes = _map(auth_page).locator(".svelte-flow__node")
    assert nodes.count() > 5
    expect(_map(auth_page).locator(".node", has_text="web-app").first).to_be_visible()


def test_incoming_link_is_shown(auth_page: Page, app_server: str) -> None:
    """Hij komt uit het bestand van het ANDERE project, niet uit dit project."""
    _open(auth_page, app_server, f"/projects/{PROJECT}/kaart")
    expect(_map(auth_page).locator(".node", has_text="backend").first).to_be_visible()


def test_click_on_component_opens_no_panel(auth_page: Page, app_server: str) -> None:
    _open(auth_page, app_server, f"/projects/{PROJECT}/kaart")
    _map(auth_page).locator(".node", has_text="web-app").first.click()
    auth_page.wait_for_timeout(500)
    assert _map(auth_page).locator("aside.panel").count() == 0


def test_info_button_opens_panel(auth_page: Page, app_server: str) -> None:
    _open(auth_page, app_server, f"/projects/{PROJECT}/kaart")
    _map(auth_page).locator(".node", has_text="web-app").first.locator("button.info").click()
    expect(_map(auth_page).locator("aside.panel")).to_be_visible()


def test_click_on_deployment_expands_it(auth_page: Page, app_server: str) -> None:
    _open(auth_page, app_server, f"/projects/{PROJECT}/kaart")
    zad_map = _map(auth_page)
    before = zad_map.locator(".svelte-flow__node").count()
    zad_map.locator(".node", has_text="default").first.click()
    auth_page.wait_for_timeout(2000)
    assert zad_map.locator(".svelte-flow__node").count() > before


def test_platform_page_shows_the_hub(auth_page: Page, app_server: str) -> None:
    _open(auth_page, app_server, "/platform")
    expect(_map(auth_page).locator(".node.hub", has_text="Operations Manager")).to_be_visible()
