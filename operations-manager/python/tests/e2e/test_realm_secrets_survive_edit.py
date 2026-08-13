"""Editing a project through the modal wizard may not touch the realm admin connection.

The realm admin password is written by OPI, exists nowhere else, and no form shows it.
It is redacted out of the disk-backed wizard session and put back from the stored project
at save; when that pairing failed, a services edit dropped both secrets on the realm --
``password``, which then fails validation and blocks every later edit, and ``totp_secret``,
which is optional and would have disappeared without a sound.

Driven through the real browser and the real routes, because the loss lives in the shape
the editables hand back: an in-process harness that fills the merged data itself never
produces it. The fixture project therefore carries a realm with both secrets.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from tests.e2e.helpers.edit_modal import EditModalHelper
from tests.e2e.helpers.tekst import veld

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = pytest.mark.e2e

PROJECT_NAME = "test-project-detail"


def _stored_project() -> dict[str, Any]:
    from opi.services.project_service import get_project_service

    project = get_project_service().get_project(PROJECT_NAME)
    return project.data if hasattr(project, "data") else project


def _stored_service_names() -> list[str]:
    from opi.services.services import service_entry_name

    return [service_entry_name(entry) for entry in _stored_project().get("services") or []]


def _stored_realm() -> dict[str, Any]:
    """The realm admin connection as it currently stands in the saved project."""
    for entry in _stored_project().get("services") or []:
        if isinstance(entry, dict) and (entry.get("name") == "keycloak" or "keycloak" in entry):
            body = entry.get("keycloak", entry)
            realms = (body.get("config") or {}).get("realms") or []
            return dict(realms[0]) if realms else {}
    return {}


def test_identity_edit_leaves_the_realm_admin_connection_alone(app_server: str, auth_page: Page) -> None:
    before = _stored_realm()
    assert before.get("password"), "fixture must carry an encrypted realm password"

    modal = EditModalHelper(auth_page, app_server, PROJECT_NAME)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")
    original_description = veld(modal.page, "description").input_value()
    try:
        modal.fill_field("description", "RC-102 controle")
        modal.submit_step()
        modal.wait_for_success()
    finally:
        # The project is shared with the rest of this suite and the app is session-scoped.
        modal.open_detail_page()
        modal.open_edit_modal("modal-edit-identity", "Projectgegevens bewerken")
        modal.fill_field("description", original_description)
        modal.submit_step()
        modal.wait_for_success()

    assert _stored_realm() == before


def test_services_edit_leaves_the_realm_admin_connection_alone(app_server: str, auth_page: Page) -> None:
    """The edit that lost it: selecting an unrelated service and walking to the end.

    The keycloak config step comes along because keycloak is selected, and it is that
    step's own data -- written in the name-as-key shape -- that the restore had to pair
    with the stored record.
    """
    before = _stored_realm()
    assert before.get("password"), "fixture must carry an encrypted realm password"
    assert before.get("totp_secret"), "fixture must carry an optional second secret"

    modal = EditModalHelper(auth_page, app_server, PROJECT_NAME)
    modal.open_detail_page()
    modal.open_edit_modal("modal-edit-services", "Services beheren")
    modal.toggle_service("authorization-wall")
    modal.submit_step()
    modal.advance_to_review()

    # This flow ends in a deployment, so the confirm swaps in a progress view instead of
    # settling: wait for the save request itself rather than for the page to go quiet.
    confirm = modal.page.locator(
        ".wizard-review button[type='submit'], button:has-text('Bevestigen'), button:has-text('Opslaan')"
    ).first
    with modal.page.expect_response(
        lambda response: response.request.method == "POST" and response.url.split("?")[0].endswith("/confirm")
    ):
        confirm.click()

    # Two ways this can go wrong and only both assertions together catch them: the save is
    # refused (the project file keeps its realm, but no edit gets through any more), or the
    # save lands and takes the secrets with it. So check that the edit arrived AND that the
    # realm is untouched. The confirm's own status is no signal here -- this flow starts a
    # deployment afterwards, which has no cluster to talk to in a local run.
    assert "authorization-wall" in _stored_service_names(), "the services edit was refused"
    assert _stored_realm() == before
