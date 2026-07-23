"""
E2E tests for full wizard project creation against a running sandbox.

These tests require:
- A running sandbox cluster (Kind + all infrastructure)
- E2E_BASE_URL environment variable set (e.g., https://zad.sandbox.rijksapp.dev)
- Optionally E2E_SECRET_KEY if the sandbox uses a non-default secret

Run with: pytest tests/e2e/ -m "e2e and sandbox" -v --timeout=300
"""

from typing import TYPE_CHECKING

import pytest
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.cleanup import ProjectCleanup
from tests.e2e.helpers.lifecycle import create_project_via_wizard
from tests.e2e.helpers.wizard import WizardHelper, _unique_project_name

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]


def test_wizard_create_verified_in_forgejo(
    sandbox_url: str,
    sandbox_page: "Page",
    forgejo: "ForgejoClient",
) -> None:
    """Create a project via the real wizard, then verify it in the Forgejo git repo.

    Forgejo (zad-projects) is the authoritative "did it work?" source -- the wizard's
    HTTP response is never trusted on its own. The technical project id is server-
    generated (a random slug, not the display name), so it is resolved by diffing the
    repo listing before/after (create_project_via_wizard). Cleaned up via the API.
    """
    display = _unique_project_name()
    project = create_project_via_wizard(
        sandbox_page,
        sandbox_url,
        forgejo,
        display_name=display,
        user_email="admin@sandbox.rijksapp.dev",
    )
    try:
        assert forgejo.project_file_exists(project.name), f"'{project.name}' not committed to zad-projects"
        assert "web" in forgejo.component_names(project.name), "component 'web' not in the committed project"
        data = forgejo.get_project_yaml(project.name)
        assert data and data.get("components"), "committed project yaml has no components"
    finally:
        # Force teardown so a partial/207 delete still cleans the sandbox.
        sandbox_api.delete_project_via_api(sandbox_url, project.name, project.api_key, verify_ssl=False)


@pytest.fixture
def cleanup(sandbox_page: Page, sandbox_url: str) -> ProjectCleanup:
    """Project cleanup fixture - cleans up after test completes."""
    tracker = ProjectCleanup()
    yield tracker
    tracker.cleanup_via_ui(sandbox_page, sandbox_url)


def test_sandbox_reachable(sandbox_url: str, sandbox_page: Page) -> None:
    """Verify the sandbox cluster is reachable via the configured URL."""
    response = sandbox_page.goto(f"{sandbox_url}/health")
    assert response is not None
    assert response.status == 200


def test_sandbox_authenticated(sandbox_url: str, sandbox_page: Page) -> None:
    """Verify the pre-signed session cookie grants access to protected pages."""
    sandbox_page.goto(sandbox_url)
    sandbox_page.wait_for_load_state("networkidle")
    # Should not be redirected to login
    assert "/auth/login" not in sandbox_page.url
    assert "/permission-denied" not in sandbox_page.url


def test_wizard_loads_on_sandbox(sandbox_url: str, sandbox_page: Page) -> None:
    """The create-project wizard loads on the sandbox cluster."""
    wizard = WizardHelper(sandbox_page, sandbox_url)
    wizard.open_create_wizard()
    assert "/forms/wizard/create-project" in sandbox_page.url


def test_wizard_minimal_project(
    sandbox_url: str,
    sandbox_page: Page,
    cleanup: ProjectCleanup,
) -> None:
    """Walk through the wizard to create a minimal project on the sandbox.

    Steps: identity → skip services → team → component → domain → deploy.
    Verifies the wizard completes without errors.
    """
    project_name = _unique_project_name()
    wizard = WizardHelper(sandbox_page, sandbox_url)
    wizard.open_create_wizard()

    # Step 1: Identity
    wizard.fill_identity(
        display_name=project_name,
        description=f"E2E test project {project_name}",
    )
    wizard.click_next()

    # Step 2: Services - skip (no services selected), just advance
    wizard.click_next()

    # Step 3: Team - fill with sandbox admin user
    wizard.fill_team(email="admin@sandbox.rijksapp.dev")
    wizard.click_next()

    # Step 4: Components - fill minimal component
    wizard.fill_component(name="web", image="nginx:latest")
    wizard.click_next()

    # Step 5: Deployment - advance with defaults
    wizard.click_next()

    # Step 6: Web address - advance with defaults to the review page
    wizard.click_review()

    # Review page - verify we can see the project name
    sandbox_page.wait_for_load_state("networkidle")
    page_text = sandbox_page.text_content("body") or ""
    assert project_name in page_text, f"Project name '{project_name}' not found on review page"

    # Register for cleanup before submitting
    cleanup.register(project_name)

    # Submit the wizard
    wizard.submit_wizard()

    # Wait for deployment to start/complete
    sandbox_page.wait_for_load_state("networkidle")

    # Verify we're no longer on the wizard page (redirected to progress or project detail)
    assert "/forms/wizard/create-project/step/" not in sandbox_page.url


def test_wizard_project_appears_in_list(
    sandbox_url: str,
    sandbox_page: Page,
    cleanup: ProjectCleanup,
) -> None:
    """After creating a project via wizard, it appears in the projects list."""
    project_name = _unique_project_name()
    wizard = WizardHelper(sandbox_page, sandbox_url)
    wizard.open_create_wizard()

    # Fill only identity (minimal viable submission)
    wizard.fill_identity(
        display_name=project_name,
        description=f"E2E list test {project_name}",
    )
    wizard.click_next()

    # Services - skip
    wizard.click_next()

    # Team
    wizard.fill_team(email="admin@sandbox.rijksapp.dev")
    wizard.click_next()

    # Components
    wizard.fill_component(name="app", image="nginx:latest")
    wizard.click_next()

    # Domains - advance
    wizard.click_next()

    cleanup.register(project_name)
    wizard.submit_wizard()

    # Wait for submission to process
    sandbox_page.wait_for_load_state("networkidle")

    # Navigate to projects list and verify project appears
    sandbox_page.goto(f"{sandbox_url}/projects")
    sandbox_page.wait_for_load_state("networkidle")

    page_text = sandbox_page.text_content("body") or ""
    assert project_name in page_text, f"Project '{project_name}' not found in projects list"
