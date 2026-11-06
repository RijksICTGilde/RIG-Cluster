"""Verhoogde rechten: the popup from the account menu, through the real routes and templates."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from opi.utils.totp import generate_totp_secret, totp_now
from tests.e2e.testserver import ELEVATED_BY_DEFAULT, get_in_memory_otp_service

if TYPE_CHECKING:
    from collections.abc import Iterator

    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

EMAIL = "test@example.com"


@pytest.fixture(autouse=True)
def real_step_up() -> Iterator[None]:
    ELEVATED_BY_DEFAULT["on"] = False
    yield
    ELEVATED_BY_DEFAULT["on"] = True


@pytest.fixture
def secret() -> str:
    """A confirmed factor for the test user, whose code the test can compute."""
    service = get_in_memory_otp_service()
    service._rows.clear()
    service._pending.clear()
    value = generate_totp_secret()
    # Directly and not through a registration: that would burn the code this test needs.
    service._rows[EMAIL] = {
        "secret": value,
        "confirmed_at": datetime.now(UTC),
        "last_step": 0,
        "failed": 0,
        "locked_until": None,
    }
    return value


def _open_from_menu(page: Page) -> None:
    page.locator("nldd-toolbar-item nldd-icon-button[popup-type='menu']").click()
    item = page.locator("nldd-menu[slot='popup'] nldd-menu-item[text='Verhoogde rechten']")
    item.wait_for(state="visible")
    item.click()


def _give_code(page: Page, code: str) -> None:
    page.locator("#verhoog-form nldd-text-field input").fill(code)
    page.locator("#verhoog-form nldd-button[type='submit']").click()


def test_an_admin_page_opens_the_popup_on_the_dashboard(app_server: str, auth_page: Page, secret: str) -> None:
    auth_page.goto(f"{app_server}/admin/users")

    auth_page.locator("#verhoog-form").wait_for(state="visible")
    assert auth_page.url.endswith("/dashboard")


def test_without_a_step_up_the_sidebar_has_no_admin_items(app_server: str, auth_page: Page) -> None:
    auth_page.goto(f"{app_server}/dashboard")

    assert auth_page.locator("nldd-menu-item[text='Verhoogde rechten']").count() >= 1
    assert 'href="/admin/users"' not in auth_page.content()


def test_without_a_factor_the_popup_points_to_the_enrolment(app_server: str, auth_page: Page) -> None:
    get_in_memory_otp_service()._rows.clear()
    auth_page.goto(f"{app_server}/dashboard")

    _open_from_menu(auth_page)

    auth_page.locator("#verhoog-inhoud nldd-button[text='Factor inrichten']").wait_for(state="visible")


def test_a_wrong_code_shows_the_error_in_the_popup(app_server: str, auth_page: Page, secret: str) -> None:
    auth_page.goto(f"{app_server}/dashboard")
    _open_from_menu(auth_page)

    _give_code(auth_page, "000000")

    auth_page.locator("#verhoog-code-fout").wait_for(state="visible")
    assert "Die code klopt niet" in auth_page.locator("#verhoog-inhoud").inner_text()
    assert auth_page.url.endswith("/dashboard")


def test_cancel_closes_the_popup(app_server: str, auth_page: Page, secret: str) -> None:
    auth_page.goto(f"{app_server}/dashboard")
    _open_from_menu(auth_page)

    auth_page.locator("#verhoog-form nldd-button[text='Annuleren']").click()

    auth_page.locator("#verhoog-form").wait_for(state="hidden")


def test_a_valid_code_returns_to_the_admin_page_and_ending_closes_it(
    app_server: str, auth_page: Page, secret: str
) -> None:
    auth_page.goto(f"{app_server}/admin/users")
    auth_page.locator("#verhoog-form").wait_for(state="visible")

    _give_code(auth_page, totp_now(secret)[0])

    auth_page.wait_for_url(lambda url: url.endswith("/admin/users"))

    _open_from_menu(auth_page)
    auth_page.locator("#verhoog-inhoud nldd-button[text='Beëindigen']").click()
    auth_page.wait_for_url(lambda url: url.endswith("/dashboard"))

    auth_page.goto(f"{app_server}/admin/users")
    auth_page.locator("#verhoog-form").wait_for(state="visible")
