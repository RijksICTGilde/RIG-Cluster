"""LEVEL 5 (UI, Playwright): a project with a registry gets through the create wizard.

Reported in production: the wizard finished, and the create task then failed with
"registries, nummer 1, password: String should match pattern ...". In the create wizard the
project has no AGE key yet, so the token was handed to the task as typed, and the store's
validation refused it. Nobody had walked this path to the end: until 3 November the add
button of the registry step already failed with a 500.

The test server skips the store's validation, so the project file handed to the create
task is captured and put through ``validate_service_configs``, the check that refused it.

Run: uv run pytest tests/e2e/test_wizard_registry_token_encrypted.py -m "e2e and not sandbox" -q
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
import yaml
from opi.manager.project_validation import validate_service_configs
from opi.services.catalog.image_registries.resolution import project_registries
from tests.e2e.helpers.wizard import (
    WizardHelper,
    kies_verplichte_keuzelijsten,
    unique_project_name,
    veldbesturing_eindigend_op,
    voeg_reeksitem_toe,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

pytestmark = [pytest.mark.e2e]

SERVICE = "image-registries"
STEP = f"{SERVICE}-config"
TOKEN = "e2e-secret-token"
_MAX_WIZARD_STEPS = 15


@pytest.fixture
def captured_yaml(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The project YAML the wizard hands to the create task."""
    captured: list[str] = []

    import opi.core.task_helpers as task_helpers

    async def _fake_create_async_task(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs["payload"]["yaml_content"])
        return {"task_id": "00000000-0000-0000-0000-000000000000"}

    monkeypatch.setattr(task_helpers, "create_async_task", _fake_create_async_task)
    return captured


@pytest.fixture(autouse=True)
def _token_check_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token check talks to the real registry; this test is about what is stored."""
    import opi.services.catalog.image_registries.enforcers as enforcers

    connector = SimpleNamespace(
        check_login=AsyncMock(return_value=(True, "")),
        check_repository_access=AsyncMock(return_value=(True, "")),
    )
    monkeypatch.setattr(enforcers, "_connector", lambda: connector)


def _step(page: Page) -> str:
    return page.url.rsplit("/step/", 1)[-1]


def _walk(wizard: WizardHelper, page: Page, until: str | None) -> None:
    """Click on until *until* (or the review page), filling what blocks the way."""
    for _ in range(_MAX_WIZARD_STEPS):
        if until is None and page.locator("#wizard-review-submit").count() > 0:
            return
        if until is not None and _step(page) == until:
            return
        if _step(page) == "team":
            wizard.fill_team(email="test@example.com")
        elif _step(page) == "components":
            wizard.fill_component(name="web", image="ghcr.io/minbzk/e2e-private:1")
        kies_verplichte_keuzelijsten(page)
        wizard.click_next()
        page.wait_for_load_state("networkidle")
    raise AssertionError(f"did not reach {until or 'the review page'}; stuck at {page.url}")


def test_the_registry_token_reaches_the_create_task_encrypted(
    app_server: str, auth_page: Page, captured_yaml: list[str]
) -> None:
    wizard = WizardHelper(auth_page, app_server)
    wizard.open_create_wizard()
    wizard.fill_identity(display_name=unique_project_name(), description="registry token encrypted")
    wizard.click_next()
    wizard.fill_services([SERVICE])
    _walk(wizard, auth_page, STEP)

    voeg_reeksitem_toe(auth_page, "registries")
    password = veldbesturing_eindigend_op(auth_page, "registries[0]/password")
    password.wait_for(state="visible", timeout=10000)
    veldbesturing_eindigend_op(auth_page, "registries[0]/display-name").fill("GitHub MinBZK")
    veldbesturing_eindigend_op(auth_page, "registries[0]/upstream").fill(
        "https://github.com/orgs/MinBZK/packages/container/package/e2e-private"
    )
    password.fill(TOKEN)

    wizard.click_next()
    _walk(wizard, auth_page, None)
    wizard.submit_wizard()

    assert captured_yaml, "the wizard did not hand a project to the create task"
    project = yaml.safe_load(captured_yaml[-1])
    (registry,) = project_registries(project)
    assert registry["upstream"] == "ghcr.io/minbzk"
    assert registry["password"].startswith("-----BEGIN AGE ENCRYPTED FILE-----"), (
        "the token reached the create task as typed"
    )
    validate_service_configs(project)
