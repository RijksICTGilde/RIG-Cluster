"""
E2E tests verifying ROOS components are fully preprocessed to HTML.

Catches regressions where <c-*> tags pass through unconverted due to
syntax issues or preprocessor failures. Every page using ROOS components
should be checked here.
"""

import re
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

RAW_ROOS_PATTERN = re.compile(r"<c-[a-z][a-z-]+")

DETAIL_URL = "/projects/details/test-project-detail"
TOOLS_URL = "/tools"
WIZARD_URL = "/forms/wizard/create-project"
DASHBOARD_URL = "/dashboard"


def _assert_no_raw_roos_tags(page: Page, url_label: str) -> None:
    """Assert that no unconverted <c-*> tags remain in the rendered HTML."""
    html = page.content()
    raw_tags = RAW_ROOS_PATTERN.findall(html)
    if raw_tags:
        # Show context around each match for debugging
        contexts = []
        for tag in sorted(set(raw_tags)):
            idx = html.find(tag)
            snippet = html[max(0, idx - 60) : idx + 80]
            contexts.append(f"  {tag}: ...{snippet}...")
        pytest.fail(
            f"Raw ROOS component tags found on {url_label}: {sorted(set(raw_tags))}\nContext:\n" + "\n".join(contexts)
        )


def test_detail_page_no_raw_roos(app_server: str, auth_page: Page) -> None:
    """Detail page: all ROOS components are converted to HTML."""
    auth_page.goto(f"{app_server}{DETAIL_URL}")
    auth_page.wait_for_load_state("networkidle")
    _assert_no_raw_roos_tags(auth_page, DETAIL_URL)


def test_tools_page_no_raw_roos(app_server: str, auth_page: Page) -> None:
    """Tools page: all ROOS components are converted to HTML."""
    auth_page.goto(f"{app_server}{TOOLS_URL}")
    auth_page.wait_for_load_state("networkidle")
    _assert_no_raw_roos_tags(auth_page, TOOLS_URL)


def test_wizard_step1_no_raw_roos(app_server: str, auth_page: Page) -> None:
    """Wizard identity step: all ROOS components are converted to HTML."""
    auth_page.goto(f"{app_server}{WIZARD_URL}")
    auth_page.wait_for_load_state("networkidle")
    _assert_no_raw_roos_tags(auth_page, WIZARD_URL)


def test_dashboard_no_raw_roos(app_server: str, auth_page: Page) -> None:
    """Dashboard: all ROOS components are converted to HTML."""
    auth_page.goto(f"{app_server}{DASHBOARD_URL}")
    auth_page.wait_for_load_state("networkidle")
    _assert_no_raw_roos_tags(auth_page, DASHBOARD_URL)
