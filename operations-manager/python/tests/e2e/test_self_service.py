"""
E2E tests for the self-service project creation form.
"""

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e


def test_self_service_form_renders(app_server: str, auth_page: Page) -> None:
    """With auth, /projects/new renders the self-service form."""
    response = auth_page.goto(f"{app_server}/projects/new")
    assert response is not None
    assert "/auth/login" not in auth_page.url
    assert "/permission-denied" not in auth_page.url


def test_self_service_form_has_fields(app_server: str, auth_page: Page) -> None:
    """The self-service form contains expected form elements."""
    auth_page.goto(f"{app_server}/projects/new")
    # The form should have at least one input and a submit button
    inputs = auth_page.locator("input")
    assert inputs.count() > 0
    buttons = auth_page.locator("button[type='submit'], input[type='submit']")
    assert buttons.count() > 0
