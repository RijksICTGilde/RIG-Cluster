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


def test_root_redirects_to_architecture(app_server: str, page: Page) -> None:
    """GET / redirects to /architecture."""
    page.goto(f"{app_server}/")
    assert "/architecture" in page.url


def test_architecture_page_loads(app_server: str, page: Page) -> None:
    """GET /architecture returns a page with content."""
    response = page.goto(f"{app_server}/architecture")
    assert response is not None
    assert response.ok
