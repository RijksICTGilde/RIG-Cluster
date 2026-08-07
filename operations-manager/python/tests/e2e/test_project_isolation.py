"""E2E tests for the isolation the suite itself depends on (RC-50).

The app, the browser context and the in-memory project registry are all
session-scoped, so what one test writes is still there for the next one. These
tests pin down the two things that make that harmless:

1. every test file works on its own project, derived from the file name;
2. whatever a test writes is put back before the next test runs.

Every test here is self-contained on purpose: it first asserts that it started
from a clean state and only then makes a mess. That is what a guard against
leaking state has to look like, because it must hold in *any* order. An earlier
version used ``_a_``/``_b_`` pairs -- one test dirtied, the next asserted it was
gone -- which only works if the runner keeps them in file order. Shuffle the
suite (``task test-e2e-random``) and those pairs fail on their own ordering
rather than on the thing they guard, which is worse than no guard at all: the
suite that is supposed to prove order-independence becomes the reason it is
lost.

The repeated variants are not padding, and the two mechanisms are guarded by
different ones. Measured with the reset fixture disabled (seeds 1 and 2): only
``test_a_stray_project_never_reaches_the_next_test`` runs 2 and 3 fail -- those
are the ones that hold the reset to account. The
``test_this_files_project_starts_from_the_fixture_state`` variants keep passing
without it, because ``own_project`` registers a fresh copy for every test and
that overwrites the dirtied one anyway; what they guard is that per-file
project, not the reset.
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
DIRTY_DESCRIPTION = "Aangepast door test_project_isolation"
STRAY_PROJECT = "e2e-stray-left-behind"


def test_own_project_is_this_files_own(own_project: str) -> None:
    """The project name comes from this file, not from a name shared with others."""
    assert own_project == "e2e-project-isolation"
    assert own_project != PROJECT_TEMPLATE


@pytest.mark.parametrize("run", [1, 2, 3])
def test_a_stray_project_never_reaches_the_next_test(app_server: str, run: int) -> None:
    """A project a test leaves behind is gone before the next test starts.

    Stand-in for what the wizard suites do: save a project into the session-wide
    registry and move on. Nothing here removes it -- the reset fixture does, and
    this test asserts that on the way in, before adding one of its own. So it
    guards the same thing whichever of the three runs first.
    """
    service = get_project_service()
    assert service.get_project(STRAY_PROJECT) is None, "a project left behind by an earlier test survived into this one"

    service.register(STRAY_PROJECT, "stray-key", f"{STRAY_PROJECT}.yaml", [], {"name": STRAY_PROJECT})
    assert service.get_project(STRAY_PROJECT) is not None


@pytest.mark.parametrize("run", [1, 2])
def test_this_files_project_starts_from_the_fixture_state(
    app_server: str, auth_page: Page, own_project: str, run: int
) -> None:
    """An edit to this file's own project does not reach the next test.

    Same shape: assert clean, then dirty it. The edit is really saved (asserted
    below), so if anything carried it over, the other run would see it.
    """
    auth_page.goto(f"{app_server}/projects/details/{own_project}")
    auth_page.wait_for_load_state("networkidle")
    body = auth_page.text_content("body") or ""
    assert DIRTY_DESCRIPTION not in body, "an earlier test's edit survived into this one"
    assert PRISTINE_DESCRIPTION in body

    modal = EditModalHelper(auth_page, app_server, own_project)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")
    modal.fill_field("description", DIRTY_DESCRIPTION)
    modal.submit_step()
    modal.wait_for_success()

    modal.open_detail_page()
    assert DIRTY_DESCRIPTION in (auth_page.text_content("body") or ""), "the edit was not saved, so this proves nothing"


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
