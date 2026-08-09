"""
E2E tests for the project detail page.

Tests that the detail page renders correctly with AGE-encrypted project data,
showing components, team members, services, and deployments.
"""

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers.tabs import open_tab
from tests.e2e.helpers.tekst import toon_tekst

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

DETAIL_URL = "/projects/details/test-project-detail"


def test_detail_page_renders(app_server: str, auth_page: Page) -> None:
    """Navigate to detail page, verify it loads with project info."""
    response = auth_page.goto(f"{app_server}{DETAIL_URL}")
    assert response is not None
    assert response.ok, f"Detail page returned {response.status}"

    # Should not redirect
    assert "details/test-project-detail" in auth_page.url

    # Check project name/description appear
    toon_tekst(auth_page, "Detail Test Project")
    toon_tekst(auth_page, "Uitgebreid testproject")


def test_detail_page_shows_components(app_server: str, auth_page: Page) -> None:
    """Verify component section shows component names."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")

    toon_tekst(auth_page, "web-app")
    toon_tekst(auth_page, "worker")


def test_detail_page_shows_team(app_server: str, auth_page: Page) -> None:
    """Verify team section lists users and roles."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")

    toon_tekst(auth_page, "test@example.com")
    toon_tekst(auth_page, "developer@example.com")


def test_detail_page_shows_services(app_server: str, auth_page: Page) -> None:
    """Verify service badges appear for configured services."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")

    body = auth_page.text_content("body") or ""
    # The project has keycloak and publish-on-web services
    assert "keycloak" in body.lower()


def test_detail_page_shows_deployments(app_server: str, auth_page: Page) -> None:
    """Verify deployment section shows deployment info.

    Het tabblad Deployments wordt eerst geopend. Op de bestaande pagina staan alle
    tabbladen in EEN document en is dat overbodig; op de nieuwe heeft elk tabblad een
    eigen URL en staan de deployments er pas als je erheen gaat. open_tab() dekt beide.
    """
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")
    open_tab(auth_page, "deployments")

    toon_tekst(auth_page, "default")
    toon_tekst(auth_page, "local")


def test_service_contributed_blocks_render(app_server: str, auth_page: Page) -> None:
    """RC-24: the blocks the project's services own actually reach the page.

    They are gathered per project/deployment rather than hardcoded, so a wiring mistake
    shows up as a silently missing block -- which reads like a project that does not use
    the service. This asserts the plumbing end to end for both hooks: the project-level
    Keycloak block and, on the Deployments tab, the database service's action buttons
    plus their modal opener.
    """
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")
    toon_tekst(auth_page.locator("#tab-project"), "Keycloak")

    open_tab(auth_page, "deployments")
    auth_page.locator("#tab-deployments").wait_for(state="visible", timeout=5000)
    tabblad = auth_page.locator("#tab-deployments")
    toon_tekst(tabblad, "Databaseconsole")
    toon_tekst(tabblad, "Job uitvoeren")
    # The buttons call the shared opener; without it they would render but do nothing.
    assert auth_page.evaluate("typeof openServiceModal") == "function"


def test_detail_page_screenshot(app_server: str, auth_page: Page, screenshot_dir: Path) -> None:
    """Take full-page screenshot of the detail page."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")

    path = screenshot_dir / "detail-page.png"
    auth_page.screenshot(path=path, full_page=True)
    assert path.exists()
