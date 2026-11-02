"""De kaart in de browser: het tabblad, het paneel, het uitklappen en /platform.

Dit is de enige meting die het ELEMENT echt draait, onder de CSP die de app zelf stuurt.
Een bundel die daar niet langs komt rendert niets en geeft geen foutmelding op het scherm,
dus een groene unittest op de graaf zegt hier niets over.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Route, expect

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


WEB_ADDRESS = "https://web-app.example.org/"


def _with_web_address(route: Route) -> None:
    """Add a web address to the first deployment component: the fixture project has none."""
    graph = route.fetch().json()
    deployment = next(node for node in graph["nodes"] if node["id"] == "d:default")
    deployment["children"][0]["rows"].append({"icon": "globe", "text": "web-app.example.org", "href": WEB_ADDRESS})
    route.fulfill(json=graph)


def test_web_address_in_deployment_opens_new_tab(auth_page: Page, app_server: str) -> None:
    """Clicking the web address opens a new tab and leaves the map untouched."""
    auth_page.route("**/kaart.json", _with_web_address)
    auth_page.context.route(WEB_ADDRESS, lambda route: route.fulfill(body="ok"))
    _open(auth_page, app_server, f"/projects/{PROJECT}/kaart")
    zad_map = _map(auth_page)
    zad_map.locator(".node", has_text="default").first.click()
    link = zad_map.locator("a.row.web").first
    expect(link).to_be_visible(timeout=TIMEOUT)
    expect(link).to_have_attribute("href", WEB_ADDRESS)
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")
    nodes_before = zad_map.locator(".svelte-flow__node").count()
    with auth_page.context.expect_page() as new_tab:
        link.click()
    assert new_tab.value.url == WEB_ADDRESS
    assert zad_map.locator(".svelte-flow__node").count() == nodes_before
