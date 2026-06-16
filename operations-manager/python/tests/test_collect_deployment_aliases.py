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

    async def get_contents() -> dict:
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
