"""
E2E tests for navigation, authentication redirects, and authenticated page loads.
"""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


def test_unauthenticated_redirects_to_login(app_server: str, page: Page) -> None:
    """Without auth cookie, accessing a protected page redirects to /auth/login."""
    page.goto(f"{app_server}/dashboard")
    assert "/auth/login" in page.url


def test_authenticated_dashboard_loads(app_server: str, auth_page: Page) -> None:
    """With auth cookie, /dashboard loads successfully."""
    response = auth_page.goto(f"{app_server}/dashboard")
    assert response is not None
    # Should not redirect to login
    assert "/auth/login" not in auth_page.url


def test_permission_denied_page(app_server: str, page: Page) -> None:
    """/permission-denied renders the template."""
    response = page.goto(f"{app_server}/permission-denied")
    assert response is not None
    assert response.ok
