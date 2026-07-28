"""Regression test for _collect_deployment_aliases conflict handling.

Aliases are declared per component but merged into ONE shared secret per service for
the deployment. Two components declaring the same alias name is expected for shared
services and fine when the value matches (logged at DEBUG). A DIFFERENT value is
unresolvable - a single shared secret cannot hold two values - so it must raise.
"""

import re

import pytest
from opi.manager.project_manager import ProjectManager


def _pm(component_defs: dict[str, dict]) -> ProjectManager:
    """A ProjectManager with just the three methods _collect_deployment_aliases uses."""
    pm = ProjectManager.__new__(ProjectManager)

    async def get_contents(*, record_base: bool = True) -> dict:
        # record_base mirrors the real signature: projection helpers such as
        # _collect_deployment_aliases read without recording a compare-and-swap base.
        return {"deployments": [{"name": "dep1", "components": [{"reference": n} for n in component_defs]}]}

    async def get_by_json_path(expr: str) -> dict:
        name = re.search(r"name=='([^']+)'", expr).group(1)
        return component_defs[name]

    pm.get_contents = get_contents  # type: ignore[method-assign]
    pm._get_by_json_path = get_by_json_path  # type: ignore[method-assign]
    pm._categorize_alias = lambda _name, _tmpl: ("database", "secret")  # type: ignore[method-assign]
    return pm


@pytest.mark.asyncio
async def test_identical_alias_across_components_is_allowed() -> None:
    pm = _pm(
        {
            "a": {"name": "a", "aliases": {"POSTGRES_DB": "{{ db.name }}"}},
            "b": {"name": "b", "aliases": {"POSTGRES_DB": "{{ db.name }}"}},
        }
    )
    result = await pm._collect_deployment_aliases("dep1")
    assert result["secret"]["database"]["POSTGRES_DB"] == "{{ db.name }}"


@pytest.mark.asyncio
async def test_conflicting_alias_across_components_raises() -> None:
    pm = _pm(
        {
            "a": {"name": "a", "aliases": {"POSTGRES_DB": "{{ db.name }}"}},
            "b": {"name": "b", "aliases": {"POSTGRES_DB": "{{ db.other }}"}},
        }
    )
    with pytest.raises(ValueError, match="Conflicting alias 'POSTGRES_DB'"):
        await pm._collect_deployment_aliases("dep1")


# ---------------------------------------------------------------------------
# Direct aliases are per component, not per deployment
# ---------------------------------------------------------------------------

PROJECT_DATA = {
    "components": [
        {
            "name": "headscale",
            "aliases": {
                "HEADSCALE_SERVER_URL": "https://${PUBLIC_HOSTNAME}",
                "HEADSCALE_NOISE_PRIVATE_KEY_PATH": "${DATA_PATH}/noise_private.key",
            },
        },
        # No services and no aliases: this is the component that used to blow up.
        {"name": "vlam-gateway"},
    ]
}

DEPLOYMENT_WIDE_DIRECT = {
    "web": {"HEADSCALE_SERVER_URL": "https://${PUBLIC_HOSTNAME}"},
    "storage": {"HEADSCALE_NOISE_PRIVATE_KEY_PATH": "${DATA_PATH}/noise_private.key"},
}


def test_declaring_component_keeps_its_direct_aliases() -> None:
    scoped = ProjectManager._scope_direct_aliases_to_component(DEPLOYMENT_WIDE_DIRECT, PROJECT_DATA, "headscale")
    assert scoped == DEPLOYMENT_WIDE_DIRECT


def test_sibling_without_aliases_gets_none() -> None:
    """A component that declares no aliases must not inherit a sibling's.

    Direct aliases resolve against the component's own env vars. Passing the
    deployment-wide set to a component with neither publish-on-web nor storage
    resolved them against an empty context and raised.
    """
    scoped = ProjectManager._scope_direct_aliases_to_component(DEPLOYMENT_WIDE_DIRECT, PROJECT_DATA, "vlam-gateway")
    assert scoped == {}


def test_partial_overlap_keeps_only_declared_aliases() -> None:
    project_data = {"components": [{"name": "worker", "aliases": {"WORKER_PATH": "${DATA_PATH}/w"}}]}
    direct = {
        "storage": {"WORKER_PATH": "${DATA_PATH}/w", "OTHER_PATH": "${DATA_PATH}/o"},
        "web": {"OTHER_URL": "https://${PUBLIC_HOSTNAME}"},
    }
    scoped = ProjectManager._scope_direct_aliases_to_component(direct, project_data, "worker")
    # Empty categories are dropped entirely, not left as {}.
    assert scoped == {"storage": {"WORKER_PATH": "${DATA_PATH}/w"}}
