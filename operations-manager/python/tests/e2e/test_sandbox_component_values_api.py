"""Live sandbox E2E for the env-vars/aliases API (RC-55).

Unit tests prove the storage shapes and the write layer against a real ``age`` binary
on a project dict. What only a real cluster proves is the part in between: that the
endpoint, the async task, the worker handler and ``ProjectStore`` together land the
right bytes in the ``zad-projects`` repo -- and, crucially, that nothing plaintext lands
there. This is a write path for secrets; "the unit test passed" is not the same claim.

The assertion of record is the project YAML in Forgejo, never the HTTP response. Every
call goes through the real endpoint with the project's own API key.

Skips when E2E_BASE_URL is unset.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import httpx
import pytest
from tests.e2e.helpers import sandbox_api
from tests.e2e.helpers.lifecycle import create_project_via_wizard

if TYPE_CHECKING:
    from tests.e2e.helpers.forgejo import ForgejoClient

pytestmark = [pytest.mark.e2e, pytest.mark.sandbox]

_VERIFY_SSL = os.environ.get("E2E_API_VERIFY_SSL", "false").lower() in ("1", "true", "yes")
_USER_EMAIL = os.environ.get("E2E_SANDBOX_USER", "admin@sandbox.rijksapp.dev")
_AGE_ARMOR = "-----BEGIN AGE ENCRYPTED FILE-----"

#: Distinctive enough that finding it anywhere in the repo is unambiguous.
_SECRET = "rc55-plaintext-must-never-appear"


@pytest.fixture(scope="module")
def values_project(sandbox_context, sandbox_url: str, forgejo: ForgejoClient):
    """One project created through the real wizard, deleted afterwards."""
    page = sandbox_context.new_page()
    try:
        project = create_project_via_wizard(page, sandbox_url, forgejo, display_name="waarden", user_email=_USER_EMAIL)
    finally:
        page.close()
    try:
        yield project
    finally:
        sandbox_api.delete_project_via_api(sandbox_url, project.name, project.api_key, verify_ssl=_VERIFY_SSL)


def _call(project, sandbox_url: str, method: str, path: str, body: dict | None = None) -> dict:
    """Fire one values request and wait for its task, asserting it succeeded."""
    task_id = sandbox_api.start_task(sandbox_url, method, path, project.api_key, body or {}, verify_ssl=_VERIFY_SSL)
    return sandbox_api.wait_for_task(sandbox_url, task_id, project.api_key, verify_ssl=_VERIFY_SSL)


def _component(forgejo: ForgejoClient, project_name: str, component_name: str = "web") -> dict:
    data = forgejo.get_project_yaml(project_name) or {}
    for component in data.get("components") or []:
        if component.get("name") == component_name:
            return component
    return {}


def _deployment_component(forgejo: ForgejoClient, project_name: str, component_name: str = "web") -> dict:
    data = forgejo.get_project_yaml(project_name) or {}
    for deployment in data.get("deployments") or []:
        for component in deployment.get("components") or []:
            if component.get("reference") == component_name:
                return component
    return {}


def test_env_vars_round_trip_on_the_component(values_project, sandbox_url: str, forgejo: ForgejoClient) -> None:
    """Add, patch, delete one, delete the rest -- each verified in the project file."""
    base = f"/api/v2/projects/{values_project.name}/services/user-env-vars/values/component/web"

    _call(values_project, sandbox_url, "POST", f"{base}?rollout=false", {"values": {"TOKEN": _SECRET, "MODE": "a"}})
    stored = _component(forgejo, values_project.name).get("user-env-vars")
    assert stored, "the env vars never reached the project file"
    assert _AGE_ARMOR in str(stored), f"user-env-vars is not AGE-encrypted: {str(stored)[:60]}"
    assert str(stored).count("BEGIN AGE ENCRYPTED FILE") == 1, "one block for the set, not one per entry"
    assert _SECRET not in str(stored), "the plaintext value is in the project file"

    _call(values_project, sandbox_url, "PATCH", f"{base}?rollout=false", {"values": {"MODE": "b"}})
    _call(values_project, sandbox_url, "DELETE", f"{base}/MODE?rollout=false")
    _call(values_project, sandbox_url, "DELETE", f"{base}?rollout=false")

    assert "user-env-vars" not in _component(forgejo, values_project.name), (
        "clearing left an empty property behind instead of removing it"
    )


def test_aliases_keep_readable_names_and_refuse_a_value_without_a_reference(
    values_project, sandbox_url: str, forgejo: ForgejoClient
) -> None:
    """The other storage shape, op de laag die aliassen als enige hebben.

    Deze test eiste dat elke aliaswaarde AGE-versleuteld in het bestand stond, met
    ``_SECRET`` als waarde. Dat kan niet meer, en met opzet: een alias IS een verwijzing
    naar een platformvariabele (``validate_alias_value``), een waarde zonder ``$VAR``
    wordt geweigerd, en een verwijzing is de koppeling zelf en dus geen geheim
    (``owned_value_is_secret``). Gemeten op de sandbox gaf de oude vorm een 422 met precies
    die uitleg. Wat de vangrail nu bewaakt is wat er WEL geldt: de namen blijven leesbaar,
    elke waarde staat op zichzelf in het bestand, en een waarde zonder verwijzing komt er
    niet in.
    """
    base = f"/api/v2/projects/{values_project.name}/services/aliases/values/component/web"

    _call(
        values_project,
        sandbox_url,
        "POST",
        f"{base}?rollout=false",
        {"values": {"ONE": "$DATABASE_DB", "TWO": "$OIDC_URL"}},
    )

    stored = _component(forgejo, values_project.name).get("aliases")
    assert stored, "the aliases never reached the project file"
    assert sorted(stored) == ["ONE", "TWO"], f"alias names must stay readable: {sorted(stored)}"
    # Per waarde opgeslagen en niet als een blok: de tweede waarde hoort de eerste niet
    # te overschrijven of eraan vast te zitten.
    assert stored["ONE"] == "$DATABASE_DB", f"de verwijzing is onderweg veranderd: {stored['ONE']!r}"
    assert stored["TWO"] == "$OIDC_URL", f"de verwijzing is onderweg veranderd: {stored['TWO']!r}"

    # De harde regel, op de echte schrijfweg: geen verwijzing, geen alias.
    afgewezen = httpx.request(
        "POST",
        f"{sandbox_url.rstrip('/')}{base}?rollout=false",
        json={"values": {"DRIE": _SECRET}},
        headers={"X-API-Key": values_project.api_key, "Content-Type": "application/json"},
        verify=_VERIFY_SSL,
        timeout=30.0,
    )
    assert afgewezen.status_code == 422, (
        f"een aliaswaarde zonder platformverwijzing hoort geweigerd te worden, kreeg {afgewezen.status_code}"
    )
    assert "DRIE" not in (_component(forgejo, values_project.name).get("aliases") or {})

    _call(values_project, sandbox_url, "POST", f"{base}/:delete?rollout=false", {"keys": ["ONE", "TWO"]})
    assert "aliases" not in _component(forgejo, values_project.name)


def test_the_two_env_var_layers_stay_separate(values_project, sandbox_url: str, forgejo: ForgejoClient) -> None:
    """Finding 5 of the 7 August sandbox run, on the real write path.

    The two layers are merged at deploy time with the deployment-component winning per
    key, and that merge only means anything while both values are still stored in their
    own place.
    """
    project = values_project.name
    deployment = values_project.deployment_name
    component_base = f"/api/v2/projects/{project}/services/user-env-vars/values/component/web"
    deployment_base = f"/api/v2/projects/{project}/services/user-env-vars/values/deployment/{deployment}/component/web"

    _call(values_project, sandbox_url, "POST", f"{component_base}?rollout=false", {"values": {"SHARED": "component"}})
    _call(values_project, sandbox_url, "POST", f"{deployment_base}?rollout=false", {"values": {"SHARED": "deploy"}})

    component_level = _component(forgejo, project).get("user-env-vars")
    deployment_level = _deployment_component(forgejo, project).get("user-env-vars")
    assert component_level, "writing the deployment override wiped the component-level user-env-vars"
    assert deployment_level, "the deployment override never landed"
    assert _AGE_ARMOR in str(deployment_level), "the deployment-component override is not AGE-encrypted"
    assert str(component_level) != str(deployment_level), "one write appears to have landed on both layers"

    _call(values_project, sandbox_url, "DELETE", f"{deployment_base}?rollout=false")
    assert _component(forgejo, project).get("user-env-vars"), "clearing the override cleared the component value"


def test_the_same_value_twice_does_not_commit_again(values_project, sandbox_url: str, forgejo: ForgejoClient) -> None:
    """AGE is not deterministic, so a no-op that re-encrypted would commit every call."""
    base = f"/api/v2/projects/{values_project.name}/services/aliases/values/component/web"
    _call(values_project, sandbox_url, "POST", f"{base}?rollout=false", {"values": {"NOOP": "$PUBLIC_HOST"}})
    before = _component(forgejo, values_project.name)["aliases"]["NOOP"]

    task = _call(values_project, sandbox_url, "PATCH", f"{base}?rollout=false", {"values": {"NOOP": "$PUBLIC_HOST"}})

    assert (task.get("result") or {}).get("changed") is False, "an unchanged patch reported a change"
    after = _component(forgejo, values_project.name)["aliases"]["NOOP"]
    assert after == before, "the ciphertext was rewritten for a value that did not change"

    _call(values_project, sandbox_url, "DELETE", f"{base}/NOOP?rollout=false")


def test_the_refusals_come_back_as_status_codes_not_failed_tasks(values_project, sandbox_url: str) -> None:
    """The guards a caller can act on, measured against the running server."""
    base = f"{sandbox_url.rstrip('/')}/api/v2/projects/{values_project.name}/services"
    headers = {"X-API-Key": values_project.api_key, "Content-Type": "application/json"}

    with httpx.Client(verify=_VERIFY_SSL, timeout=30.0) as client:
        no_key = client.post(f"{base}/user-env-vars/values/component/web", json={"values": {"A": "1"}})
        unknown_component = client.post(
            f"{base}/user-env-vars/values/component/nope", json={"values": {"A": "1"}}, headers=headers
        )
        bad_name = client.post(
            f"{base}/user-env-vars/values/component/web", json={"values": {"with-dash": "1"}}, headers=headers
        )
        newline_value = client.post(
            f"{base}/user-env-vars/values/component/web", json={"values": {"A": "two\nlines"}}, headers=headers
        )
        aliases_on_deployment = client.post(
            f"{base}/aliases/values/deployment/{values_project.deployment_name}/component/web",
            json={"values": {"A": "$B"}},
            headers=headers,
        )

    assert no_key.status_code == 401
    assert unknown_component.status_code == 404
    assert bad_name.status_code == 422
    assert newline_value.status_code == 422
    # No schema change: aliases live on the component only, so there is no route here --
    # a clean 404, not a 500 and not a silent write that would break the schema.
    assert aliases_on_deployment.status_code == 404
