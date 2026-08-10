"""
E2E tests for public (unauthenticated) pages.
"""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


def test_health_endpoint(app_server: str, page: Page) -> None:
    """GET /health returns ok."""
    response = page.goto(f"{app_server}/health")
    assert response is not None
    assert response.ok
    assert "ok" in page.content()


def test_root_leads_to_the_dashboard(app_server: str, page: Page) -> None:
    """GET / sends you to the dashboard.

    It used to send you to the architecture page, which is gone: an unauthenticated
    visitor lands on login from here, so what is asserted is that / does not stay on /.
    """
    page.goto(f"{app_server}/")
    assert "/architecture" not in page.url
    assert not page.url.rstrip("/").endswith(app_server.rstrip("/"))


def test_the_architecture_page_is_gone(app_server: str, auth_page: Page) -> None:
    """The page was removed; the route has to be gone with it.

    Signed in, because an anonymous visitor is sent to login before routing decides
    anything, and then a removed route looks the same as a present one.
    """
    response = auth_page.goto(f"{app_server}/architecture")
    assert response is not None
    assert response.status == 404
