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


def test_root_leads_the_visitor_to_the_introduction(app_server: str, page: Page) -> None:
    """GET / sends a visitor WITHOUT a session to the introduction.

    It used to send everyone to the dashboard, and that page requires SSO: a visitor
    without rights ended up on the login screen without ever reading what ZAD is. The
    dashboard stays the destination for a signed-in user, which is asserted below.
    """
    page.goto(f"{app_server}/")
    assert "/architecture" not in page.url
    assert page.url.rstrip("/").endswith("/introductie")


def test_root_still_leads_a_signed_in_user_to_the_dashboard(app_server: str, auth_page: Page) -> None:
    """Signed in, / keeps going to the dashboard: the introduction is for newcomers."""
    auth_page.goto(f"{app_server}/")
    assert auth_page.url.rstrip("/").endswith("/dashboard")


def test_the_architecture_page_is_gone(app_server: str, auth_page: Page) -> None:
    """The page was removed; the route has to be gone with it.

    Signed in, because an anonymous visitor is sent to login before routing decides
    anything, and then a removed route looks the same as a present one.
    """
    response = auth_page.goto(f"{app_server}/architecture")
    assert response is not None
    assert response.status == 404
