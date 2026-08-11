"""
Sandbox lifecycle E2E: create a project via the UI, add a component via the API,
delete the project via the UI - verifying each step against the project file that
lands in the Forgejo `zad-projects` repo.

Requires a running sandbox and E2E_BASE_URL (and E2E_SECRET_KEY matching the
cluster). Run with:

    task test-e2e-sandbox

or:

    E2E_BASE_URL=https://zad.sandbox.rijksapp.dev \
    E2E_SECRET_KEY=sandbox-dev-secret-key-fixed-for-stable-sessions-32min \
    uv run pytest tests/e2e/test_sandbox_flows.py -m "e2e and sandbox" -v --timeout=300
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.conftest import FORGEJO_VERIFY_SSL, SANDBOX_TEST_USER
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import (
    RUNNABLE_IMAGE,
    CreatedProject,
    create_project_via_wizard,
)
from tests.e2e.helpers.project_actions import delete_project_via_ui
from tests.e2e.helpers.wizard import _unique_project_name

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from playwright.sync_api import BrowserContext, Page
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

# Sandbox uses a real certificate; API calls verify SSL like the Forgejo client.
_API_VERIFY_SSL = FORGEJO_VERIFY_SSL

_RUNNABLE_IMAGE = RUNNABLE_IMAGE


def test_version_endpoint(sandbox_url: str) -> None:
    """The public /version endpoint reports which build is running (commit/branch/dirty)."""
    with httpx.Client(verify=_API_VERIFY_SSL, timeout=30.0) as client:
        response = client.get(f"{sandbox_url}/version")
    assert response.status_code == 200, response.text
    info = response.json()
    for key in ("name", "version", "commit", "branch", "build_date", "dirty"):
        assert key in info, f"missing '{key}' in /version response: {info}"
    logger.info("Sandbox is running build: %s", info)


@pytest.fixture(scope="module")
def lifecycle_project(
    sandbox_context: BrowserContext,
    sandbox_url: str,
    forgejo: ForgejoClient,
    artifact_dir: Path,
) -> Generator[CreatedProject]:
    """Create one project via the wizard, shared across the lifecycle tests.

    Yields the project name, its API key, and its first deployment name. Tears
    down with a best-effort API delete in case the UI-delete test did not run.
    """
    display_name = _unique_project_name()
    page = sandbox_context.new_page()
    created: CreatedProject | None = None
    try:
        created = create_project_via_wizard(
            page,
            sandbox_url,
            forgejo,
            display_name,
            user_email=SANDBOX_TEST_USER["email"],
        )
        page.screenshot(path=str(artifact_dir / f"lifecycle-{display_name}-after-create.png"), full_page=True)
        yield created
    finally:
        page.close()
        if created is not None:
            sandbox_api.delete_project_via_api(sandbox_url, created.name, created.api_key, verify_ssl=_API_VERIFY_SSL)


def test_create_project_via_ui(
    lifecycle_project: CreatedProject,
    sandbox_url: str,
    sandbox_page: Page,
    forgejo: ForgejoClient,
    capture,
) -> None:
    """The wizard-created project has a file in Forgejo and shows up in the list."""
    assert forgejo.project_file_exists(lifecycle_project.name)

    sandbox_page.goto(f"{sandbox_url}/projects")
    sandbox_page.wait_for_load_state("networkidle")
    capture(sandbox_page, "projects-list")
    body_text = sandbox_page.text_content("body") or ""
    assert lifecycle_project.name in body_text or lifecycle_project.display_name in body_text, (
        f"Project '{lifecycle_project.name}' (display '{lifecycle_project.display_name}') not found in projects list"
    )


def test_add_component_via_api(
    lifecycle_project: CreatedProject,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> None:
    """Adding a component via the v2 API lands it in the project file."""
    component_name = "apiworker"  # component names allow only lowercase letters and digits
    sandbox_api.add_component(
        sandbox_url,
        lifecycle_project.name,
        lifecycle_project.api_key,
        component_name=component_name,
        image=_RUNNABLE_IMAGE,
        deployment_names=[lifecycle_project.deployment_name],
        verify_ssl=_API_VERIFY_SSL,
        # Adding a component re-syncs the deployment, which re-refreshes the
        # user-applications app-of-apps (~90 child apps, issue #130) and waits for
        # the app to go Healthy again - minutes on the busy Kind sandbox, past the
        # 180s default.
        timeout=360.0,
    )
    assert forgejo.wait_for_component(lifecycle_project.name, component_name, timeout=120), (
        f"Component '{component_name}' did not appear in the Forgejo project file"
    )


def test_delete_component_via_api(
    lifecycle_project: CreatedProject,
    sandbox_url: str,
    forgejo: ForgejoClient,
) -> None:
    """The component added above goes again, and its deployment entry goes with it.

    The API had no way to remove a component at all (RC-73), so the last thing added
    could only be taken back through the portal. The component sits in a deployment,
    which is the normal case and the one the endpoint refuses without confirmation --
    so both answers are exercised here, against the real project file:

    1. without the flag: 409, naming the deployment that deploys it;
    2. with it: the component AND the deployment's reference to it disappear from the
       file in Forgejo. A reference left behind would make the project invalid, and the
       save is what would have rejected it.
    """
    component_name = "apiworker"

    # The endpoint answers from the same read cache the delete guard itself uses, and that
    # cache trails the commit by a moment: right after the add-component task the file in
    # Forgejo already has the component while this instance has not picked it up yet, and
    # the honest answer then is 404. Wait for the API's own view instead of racing it.
    refused_status, refused_body = 404, {}
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        refused_status, refused_body = sandbox_api.delete_component(
            sandbox_url,
            lifecycle_project.name,
            lifecycle_project.api_key,
            component_name=component_name,
            verify_ssl=_API_VERIFY_SSL,
        )
        if refused_status != 404:
            break
        time.sleep(5)

    assert refused_status == 409, f"Expected 409 for a component in use, got {refused_status}: {refused_body}"
    used_by = refused_body["detail"]["used_by"]
    assert [use["deployment"] for use in used_by] == [lifecycle_project.deployment_name], used_by

    status, task = sandbox_api.delete_component(
        sandbox_url,
        lifecycle_project.name,
        lifecycle_project.api_key,
        component_name=component_name,
        confirm_in_use=True,
        verify_ssl=_API_VERIFY_SSL,
        # Deleting reprocesses the whole project, same as adding: minutes on a busy Kind.
        timeout=360.0,
    )
    assert status == 202, f"Expected 202 for a confirmed delete, got {status}: {task}"

    def _gone(data: dict) -> bool:
        components = [c.get("name") for c in data.get("components") or []]
        references = [
            ref.get("reference")
            for deployment in data.get("deployments") or []
            for ref in deployment.get("components") or []
        ]
        return component_name not in components and component_name not in references

    assert forgejo.wait_for_condition(lifecycle_project.name, _gone, timeout=180) is not None, (
        f"Component '{component_name}' or a reference to it is still in the Forgejo project file"
    )


def test_delete_project_via_ui(
    lifecycle_project: CreatedProject,
    sandbox_url: str,
    sandbox_page: Page,
    forgejo: ForgejoClient,
    capture,
) -> None:
    """Deleting the project through the danger-zone modal removes its Forgejo file."""
    delete_project_via_ui(sandbox_page, sandbox_url, lifecycle_project.name)
    capture(sandbox_page, "delete-started")

    # The authoritative signal is the project file disappearing from Forgejo. The
    # server-side delete of a still-deploying project can take minutes.
    assert forgejo.wait_for_project_gone(lifecycle_project.name, timeout=300), (
        f"Project file for '{lifecycle_project.name}' still present in Forgejo after delete"
    )

    sandbox_page.goto(f"{sandbox_url}/projects")
    sandbox_page.wait_for_load_state("networkidle")
    capture(sandbox_page, "after-delete")
    body_text = sandbox_page.text_content("body") or ""
    assert lifecycle_project.name not in body_text, f"Project '{lifecycle_project.name}' still listed after delete"
