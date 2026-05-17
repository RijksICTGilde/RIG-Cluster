"""
E2E tests for the project detail page.

Tests that the detail page renders correctly with AGE-encrypted project data,
showing components, team members, services, and deployments.
"""

from typing import TYPE_CHECKING

import pytest

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
    body = auth_page.text_content("body") or ""
    assert "Detail Test Project" in body
    assert "Uitgebreid testproject" in body


def test_detail_page_shows_components(app_server: str, auth_page: Page) -> None:
    """Verify component section shows component names."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")

    body = auth_page.text_content("body") or ""
    assert "web-app" in body
    assert "worker" in body


def test_detail_page_shows_team(app_server: str, auth_page: Page) -> None:
    """Verify team section lists users and roles."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")

    body = auth_page.text_content("body") or ""
    assert "test@example.com" in body
    assert "developer@example.com" in body


def test_detail_page_shows_services(app_server: str, auth_page: Page) -> None:
    """Verify service badges appear for configured services."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")

    body = auth_page.text_content("body") or ""
    # The project has keycloak and publish-on-web services
    assert "keycloak" in body.lower()


def test_detail_page_shows_deployments(app_server: str, auth_page: Page) -> None:
    """Verify deployment section shows deployment info."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")

    body = auth_page.text_content("body") or ""
    assert "default" in body  # deployment name
    assert "local" in body  # cluster name


def test_detail_page_screenshot(app_server: str, auth_page: Page, screenshot_dir: Path) -> None:
    """Take full-page screenshot of the detail page."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")

    path = screenshot_dir / "detail-page.png"
    auth_page.screenshot(path=path, full_page=True)
    assert path.exists()
