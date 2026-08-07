"""E2E tests for the isolation the suite itself depends on (RC-50).

The app, the browser context and the in-memory project registry are all
session-scoped, so what one test writes is still there for the next one. These
tests pin down the two things that make that harmless:

1. every test file works on its own project, derived from the file name;
2. whatever a test writes is put back before the next test runs.

They are deliberately a pair: the first one dirties the project on purpose, the
second one asserts it came back clean. Delete the reset fixture and the second
one fails.
"""

from typing import TYPE_CHECKING

import pytest
from opi.services.project_service import get_project_service
from tests.e2e.helpers.edit_modal import EditModalHelper
from tests.e2e.testserver import load_fixture_project, rename_project

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

#: Which fixture project the own_project fixture copies for this file.
PROJECT_TEMPLATE = "test-project-detail"

PRISTINE_DESCRIPTION = "Uitgebreid testproject voor de detailpagina E2E tests"
DIRTY_DESCRIPTION = "Aangepast door test_a_a_change_lands_on_this_files_own_project"
STRAY_PROJECT = "e2e-stray-left-behind"


def test_own_project_is_this_files_own(own_project: str) -> None:
    """The project name comes from this file, not from a name shared with others."""
    assert own_project == "e2e-project-isolation"
    assert own_project != PROJECT_TEMPLATE


def test_a_a_change_lands_on_this_files_own_project(app_server: str, auth_page: Page, own_project: str) -> None:
    """Change the description and verify it is really saved (so the pair has teeth)."""
    modal = EditModalHelper(auth_page, app_server, own_project)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")
    modal.fill_field("description", DIRTY_DESCRIPTION)
    modal.submit_step()
    modal.wait_for_success()

    modal.open_detail_page()
    assert DIRTY_DESCRIPTION in (auth_page.text_content("body") or "")


def test_b_the_next_test_gets_the_project_back_clean(app_server: str, auth_page: Page, own_project: str) -> None:
    """The previous test's edit is gone: every test starts from the fixture state."""
    auth_page.goto(f"{app_server}/projects/details/{own_project}")
    auth_page.wait_for_load_state("networkidle")

    body = auth_page.text_content("body") or ""
    assert DIRTY_DESCRIPTION not in body, "the previous test's edit survived into this one"
    assert PRISTINE_DESCRIPTION in body


def test_a_a_stray_project_is_left_behind_on_purpose(app_server: str) -> None:
    """Stand-in for a test that creates a project and does not clean it up.

    The wizard suites do exactly this: they save a project into the session-wide
    registry and move on. Nothing here removes it - the next test asserts that
    the reset fixture did.
    """
    get_project_service().register(STRAY_PROJECT, "stray-key", f"{STRAY_PROJECT}.yaml", [], {"name": STRAY_PROJECT})
    assert get_project_service().get_project(STRAY_PROJECT) is not None


def test_b_the_stray_project_is_gone(app_server: str) -> None:
    """Whatever a test adds to the registry is not there for the next one."""
    assert get_project_service().get_project(STRAY_PROJECT) is None, (
        "a project left behind by the previous test survived into this one"
    )


def test_the_shared_template_project_is_never_touched(app_server: str, auth_page: Page) -> None:
    """The seeded template keeps its fixture description; files work on copies."""
    auth_page.goto(f"{app_server}/projects/details/{PROJECT_TEMPLATE}")
    auth_page.wait_for_load_state("networkidle")
    assert PRISTINE_DESCRIPTION in (auth_page.text_content("body") or "")


def test_a_copy_carries_the_name_everywhere_it_is_repeated() -> None:
    """A renamed copy must not keep pointing at the original's namespace."""
    data = rename_project(load_fixture_project(PROJECT_TEMPLATE), "e2e-copy-check")

    assert data["name"] == "e2e-copy-check"
    assert [d["namespace"] for d in data["deployments"]] == ["e2e-copy-check"]
    # The source dict is untouched: a copy, not a rename in place.
    assert load_fixture_project(PROJECT_TEMPLATE)["name"] == PROJECT_TEMPLATE
