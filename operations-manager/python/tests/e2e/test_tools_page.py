"""
E2E tests for the AGE Encryption Tools page.
"""

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

TOOLS_URL = "/tools"


def test_tools_page_renders(app_server: str, auth_page: Page) -> None:
    """Navigate to /tools, verify page loads with form fields."""
    response = auth_page.goto(f"{app_server}{TOOLS_URL}")
    assert response is not None
    assert response.ok, f"Tools page returned {response.status}"

    # Verify key form fields are present
    expect(auth_page.locator("[name='public_key']")).to_be_visible()
    expect(auth_page.locator("[name='private_key']")).to_be_visible()
    expect(auth_page.locator("[name='operation']")).to_be_visible()
    expect(auth_page.locator("[name='input_text']")).to_be_visible()

    # Verify heading
    expect(auth_page.locator("text=Configuration")).to_be_visible()


def test_tools_page_has_submit_button(app_server: str, auth_page: Page) -> None:
    """Verify the Process button is present."""
    auth_page.goto(f"{app_server}{TOOLS_URL}")
    submit_btn = auth_page.locator("#submit-btn")
    expect(submit_btn).to_be_visible()
    expect(submit_btn).to_contain_text("Process")


def test_tools_page_screenshot(app_server: str, auth_page: Page, screenshot_dir: Path) -> None:
    """Screenshot the tools page for visual verification."""
    auth_page.goto(f"{app_server}{TOOLS_URL}")
    auth_page.wait_for_load_state("networkidle")
    auth_page.screenshot(path=str(screenshot_dir / "tools-page.png"), full_page=True)
