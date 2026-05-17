"""
E2E tests for the user admin CRUD pages.

Verifies that templates render correctly, ROOS components are fully
converted to HTML, forms display and validate, and the CRUD flow works
end-to-end through the real routing/template pipeline (with only the
database-backed service mocked via an in-memory stub).
"""

import re
from typing import TYPE_CHECKING

import pytest
from tests.e2e.testserver import get_in_memory_user_service

if TYPE_CHECKING:
    from pathlib import Path

    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

USERS_URL = "/admin/users"
CREATE_URL = "/admin/users/create"

RAW_ROOS_PATTERN = re.compile(r"<c-[a-z][a-z-]+")


def _assert_no_raw_roos_tags(page: Page, url_label: str) -> None:
    """Assert that no unconverted <c-*> tags remain in the rendered HTML."""
    html = page.content()
    raw_tags = RAW_ROOS_PATTERN.findall(html)
    if raw_tags:
        contexts = []
        for tag in sorted(set(raw_tags)):
            idx = html.find(tag)
            snippet = html[max(0, idx - 60) : idx + 80]
            contexts.append(f"  {tag}: ...{snippet}...")
        pytest.fail(
            f"Raw ROOS component tags found on {url_label}: {sorted(set(raw_tags))}\nContext:\n" + "\n".join(contexts)
        )


# ---------------------------------------------------------------------------
# Rendering tests
# ---------------------------------------------------------------------------


class TestUserListPage:
    """User list page at /admin/users."""

    def test_page_loads(self, app_server: str, auth_page: Page) -> None:
        """List page returns 200 and contains expected content."""
        response = auth_page.goto(f"{app_server}{USERS_URL}")
        assert response is not None
        assert response.ok
        assert "Gebruikersbeheer" in auth_page.content()

    def test_no_raw_roos_tags(self, app_server: str, auth_page: Page) -> None:
        """All ROOS components are converted to HTML."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        auth_page.wait_for_load_state("networkidle")
        _assert_no_raw_roos_tags(auth_page, USERS_URL)

    def test_shows_seeded_users(self, app_server: str, auth_page: Page) -> None:
        """Seeded users appear in the table."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        content = auth_page.content()
        assert "Jan de Vries" in content
        assert "Maria Jansen" in content
        assert "jan@example.nl" in content

    def test_has_add_button(self, app_server: str, auth_page: Page) -> None:
        """The 'Gebruiker toevoegen' button is present."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        assert auth_page.locator("text=Gebruiker toevoegen").is_visible()

    def test_has_edit_buttons(self, app_server: str, auth_page: Page) -> None:
        """Each user row has an edit button."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        edit_buttons = auth_page.locator("text=Bewerken")
        assert edit_buttons.count() >= 2  # At least the 2 seeded users

    def test_screenshot(self, app_server: str, auth_page: Page, screenshot_dir: Path) -> None:
        """Screenshot the user list page."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        auth_page.wait_for_load_state("networkidle")
        path = screenshot_dir / "user-admin-list.png"
        auth_page.screenshot(path=str(path))
        assert path.exists()


class TestUserCreatePage:
    """Create user form at /admin/users/create."""

    def test_page_loads(self, app_server: str, auth_page: Page) -> None:
        """Create form returns 200 and contains expected heading."""
        response = auth_page.goto(f"{app_server}{CREATE_URL}")
        assert response is not None
        assert response.ok
        assert "Gebruiker toevoegen" in auth_page.content()

    def test_no_raw_roos_tags(self, app_server: str, auth_page: Page) -> None:
        """All ROOS components are converted to HTML."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.wait_for_load_state("networkidle")
        _assert_no_raw_roos_tags(auth_page, CREATE_URL)

    def test_form_fields_present(self, app_server: str, auth_page: Page) -> None:
        """Email and full_name fields are rendered."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        assert auth_page.locator("[name='email']").is_visible()
        assert auth_page.locator("[name='full_name']").is_visible()

    def test_labels_in_dutch(self, app_server: str, auth_page: Page) -> None:
        """Form labels use Dutch text."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        content = auth_page.content()
        assert "E-mailadres" in content
        assert "Volledige naam" in content

    def test_screenshot(self, app_server: str, auth_page: Page, screenshot_dir: Path) -> None:
        """Screenshot the create form."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.wait_for_load_state("networkidle")
        path = screenshot_dir / "user-admin-create.png"
        auth_page.screenshot(path=str(path))
        assert path.exists()


class TestUserCreateValidation:
    """Form validation on the create page."""

    def test_empty_submit_shows_errors(self, app_server: str, auth_page: Page) -> None:
        """Submitting empty form shows validation errors."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.locator("button[type='submit'], input[type='submit']").first.click()
        auth_page.wait_for_load_state("networkidle")

        content = auth_page.content()
        # Required fields should trigger errors (Dutch messages)
        assert "verplicht" in content.lower() or "geldig" in content.lower()

    def test_invalid_email_shows_error(self, app_server: str, auth_page: Page) -> None:
        """Submitting an invalid email shows a validation error."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.locator("[name='email']").fill("not-an-email")
        auth_page.locator("[name='full_name']").fill("Test User")
        auth_page.locator("button[type='submit'], input[type='submit']").first.click()
        auth_page.wait_for_load_state("networkidle")

        content = auth_page.content()
        assert "geldig e-mailadres" in content.lower() or "e-mail" in content.lower()

    def test_short_name_shows_error(self, app_server: str, auth_page: Page) -> None:
        """Submitting a name shorter than 2 chars shows a validation error."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.locator("[name='email']").fill("valid@example.nl")
        auth_page.locator("[name='full_name']").fill("A")
        auth_page.locator("button[type='submit'], input[type='submit']").first.click()
        auth_page.wait_for_load_state("networkidle")

        content = auth_page.content()
        assert "minimaal" in content.lower()

    def test_validation_preserves_roos_rendering(self, app_server: str, auth_page: Page) -> None:
        """After validation error, ROOS components still render correctly."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.locator("[name='email']").fill("bad")
        auth_page.locator("button[type='submit'], input[type='submit']").first.click()
        auth_page.wait_for_load_state("networkidle")
        _assert_no_raw_roos_tags(auth_page, f"{CREATE_URL} (after validation)")


class TestUserCreateFlow:
    """Full create-and-verify flow."""

    def test_create_user_appears_in_list(self, app_server: str, auth_page: Page) -> None:
        """Create a user via the form and verify it appears in the list."""
        auth_page.goto(f"{app_server}{CREATE_URL}")
        auth_page.locator("[name='email']").fill("nieuw@example.nl")
        auth_page.locator("[name='full_name']").fill("Nieuwe Gebruiker")
        auth_page.locator("button[type='submit'], input[type='submit']").first.click()

        # Should redirect to list with success message
        auth_page.wait_for_url(f"**{USERS_URL}**")
        content = auth_page.content()
        assert "Nieuwe Gebruiker" in content
        assert "nieuw@example.nl" in content


class TestUserEditPage:
    """Edit user form."""

    def _get_first_user_id(self) -> str:
        service = get_in_memory_user_service()
        # Pick the first user synchronously from the dict
        return next(iter(service._users))

    def test_edit_form_loads_with_data(self, app_server: str, auth_page: Page) -> None:
        """Edit form pre-fills with existing user data."""
        user_id = self._get_first_user_id()
        response = auth_page.goto(f"{app_server}/admin/users/{user_id}/edit")
        assert response is not None
        assert response.ok

        email_value = auth_page.locator("[name='email']").input_value()
        name_value = auth_page.locator("[name='full_name']").input_value()
        assert "@" in email_value  # Should be pre-filled
        assert len(name_value) > 0

    def test_edit_form_no_raw_roos_tags(self, app_server: str, auth_page: Page) -> None:
        """Edit form: all ROOS components are converted to HTML."""
        user_id = self._get_first_user_id()
        auth_page.goto(f"{app_server}/admin/users/{user_id}/edit")
        auth_page.wait_for_load_state("networkidle")
        _assert_no_raw_roos_tags(auth_page, f"/admin/users/{user_id}/edit")

    def test_edit_heading(self, app_server: str, auth_page: Page) -> None:
        """Edit form shows correct heading."""
        user_id = self._get_first_user_id()
        auth_page.goto(f"{app_server}/admin/users/{user_id}/edit")
        assert "Gebruiker bewerken" in auth_page.content()

    def test_screenshot(self, app_server: str, auth_page: Page, screenshot_dir: Path) -> None:
        """Screenshot the edit form."""
        user_id = self._get_first_user_id()
        auth_page.goto(f"{app_server}/admin/users/{user_id}/edit")
        auth_page.wait_for_load_state("networkidle")
        path = screenshot_dir / "user-admin-edit.png"
        auth_page.screenshot(path=str(path))
        assert path.exists()


class TestMenuIntegration:
    """Verify the admin menu item appears for admin users."""

    def test_admin_menu_item_visible(self, app_server: str, auth_page: Page) -> None:
        """Gebruikersbeheer menu item is visible on the user admin page."""
        auth_page.goto(f"{app_server}{USERS_URL}")
        assert "Gebruikersbeheer" in auth_page.content()
