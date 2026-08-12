"""Live sandbox E2E for the postgresql-database schema-list UI (RC-17) -- user-based.

Drives the real create wizard: select postgresql-database, reach the "Database-schema's"
step, add a schema item, fill its postfix, submit -- then assert the committed Forgejo
project file carries ``services/postgresql-database/config/schemas`` with that postfix.

This is the same project-level Sequence pattern that shipped invite broken in RC-13 (the
row was filled but the file got ``[]``), so it is the test that proves the schema UI
actually persists end to end. Every action is a real click or fill; no page.evaluate and
no direct modal URLs. Skips when E2E_BASE_URL is unset.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import Error as PlaywrightError
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import RUNNABLE_IMAGE, read_api_key_with_retry
from tests.e2e.helpers.wizard import (
    WizardHelper,
    veldbesturing,
    veldbesturing_eindigend_op,
    voeg_reeksitem_toe,
)

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_API_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")
_RUN = uuid.uuid4().hex[:8]
_POSTFIX = f"rap{_RUN}"


def _select_service(page: Page, name: str) -> None:
    page.wait_for_selector(f"input[name='services[]'][value='{name}']", timeout=15000)
    checkbox = page.locator(f"input[name='services[]'][value='{name}']").first
    assert checkbox.count() > 0, f"service card '{name}' not on the services step"
    if not checkbox.is_checked():
        page.locator(f"[data-service='{name}']").first.click()


def _schemas_config(forgejo: ForgejoClient, project_name: str) -> dict:
    data = forgejo.get_project_yaml(project_name) or {}
    for entry in data.get("services", []):
        if isinstance(entry, dict) and entry.get("name") == "postgresql-database":
            return entry.get("config") or {}
    return {}


def _schema_postfixes(forgejo: ForgejoClient, project_name: str) -> list[str]:
    schemas = _schemas_config(forgejo, project_name).get("schemas") or []
    return [s.get("postfix") for s in schemas if isinstance(s, dict)]


def _add_wizard_schema(page: Page) -> None:
    """On the schemas step: add one item and fill its postfix, then continue."""
    voeg_reeksitem_toe(page, "schemas")
    page.wait_for_load_state("networkidle")
    # Op de BESTURING en niet op [name$=...]: dat laatste levert het custom element op
    # en fill() daarop is een harde fout, geen leeg veld.
    veldbesturing_eindigend_op(page, "schemas[0]/postfix").first.fill(_POSTFIX)


def _walk_create_wizard(page: Page, sandbox_url: str, forgejo: ForgejoClient) -> tuple[str, str]:
    wizard = WizardHelper(page, sandbox_url)
    before = forgejo.list_project_names()
    wizard.open_create_wizard()
    wizard.fill_identity(display_name="pgschema", description="postgres schema UI e2e")
    wizard.click_next()
    _select_service(page, "postgresql-database")
    wizard.click_next()

    saw_schema_step = False
    for _ in range(20):
        page.wait_for_load_state("networkidle")
        if page.locator("button:has-text('Project aanmaken'), button:has-text('Indienen')").count() > 0:
            break
        email = veldbesturing(page, "users[0]/email")
        if email.count() > 0 and (email.first.input_value() or "") == "":
            wizard.fill_team(email=_USER_EMAIL)
        if page.locator("[name='components[0]/name']").count() > 0:
            wizard.fill_component(name="web", image=RUNNABLE_IMAGE)
        # The schemas step: its fieldset legend is unique to this section.
        if page.locator("legend:has-text('Extra schema')").count() > 0 and not saw_schema_step:
            saw_schema_step = True
            _add_wizard_schema(page)
        wizard.click_next()

    assert saw_schema_step, "the postgresql schemas step never appeared in the create wizard"
    wizard.submit_wizard()
    name = forgejo.wait_for_new_project(before, timeout=240)
    assert name, "no project appeared in Forgejo"
    return name, read_api_key_with_retry(page, sandbox_url, name)


@pytest.fixture(scope="module")
def schema_project(sandbox_context: BrowserContext, sandbox_url: str, forgejo: ForgejoClient):
    name: str | None = None
    api_key: str | None = None
    last_error: Exception | None = None
    for _ in range(3):
        page = sandbox_context.new_page()
        try:
            name, api_key = _walk_create_wizard(page, sandbox_url, forgejo)
            break
        except PlaywrightError as error:
            last_error = error
        finally:
            page.close()
    if not name or not api_key:
        pytest.fail(f"create wizard did not complete after retries: {last_error}")
    try:
        yield name
    finally:
        with contextlib.suppress(Exception):
            sandbox_api.delete_project_via_api(sandbox_url, name, api_key, verify_ssl=_API_VERIFY_SSL)


def test_wizard_wrote_postgres_schema(schema_project: str, forgejo: ForgejoClient) -> None:
    # The headline capability: adding an extra schema through the portal persists it to
    # services/postgresql-database/config/schemas (the RC-13 sequence-drop would give []).
    postfixes = _schema_postfixes(forgejo, schema_project)
    assert _POSTFIX in postfixes, f"schema postfix not persisted: {postfixes}"
