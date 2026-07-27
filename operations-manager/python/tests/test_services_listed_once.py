"""A services list may name each service at most once.

The list is a selection set keyed by service name, so a repeat is meaningless: every
reader sees only one of the entries. The wizard can no longer produce one, but a
hand-edited project file can, so validate_project_structure (the fail-closed gate both
ProjectManager and ProjectStore use before any write) rejects it.
"""

import pytest
from opi.core.project_schema import ProjectIntegrityError
from opi.manager.project_validation import validate_project_structure


def _project(**overrides) -> dict:
    data = {
        "name": "p",
        "services": ["publish-on-web", "keycloak"],
        "components": [{"name": "web", "image": "nginx:latest", "services": ["publish-on-web"]}],
        "deployments": [{"name": "productie", "components": [{"reference": "web"}]}],
    }
    data.update(overrides)
    return data


async def test_unique_services_pass():
    await validate_project_structure(_project())


async def test_duplicate_at_project_level_is_rejected():
    data = _project(
        services=["publish-on-web", {"name": "keycloak", "config": {"template": "sso-only"}}, "keycloak"],
    )
    with pytest.raises(ProjectIntegrityError, match="service 'keycloak' staat meerdere keren"):
        await validate_project_structure(data)


async def test_duplicate_on_a_component_is_rejected():
    data = _project(
        components=[{"name": "web", "image": "nginx:latest", "services": ["publish-on-web", "publish-on-web"]}],
    )
    with pytest.raises(ProjectIntegrityError, match="component 'web'"):
        await validate_project_structure(data)


async def test_duplicate_on_a_deployment_component_is_rejected():
    data = _project(
        deployments=[
            {
                "name": "productie",
                "components": [{"reference": "web", "services": [{"reference": "publish-on-web"}, "publish-on-web"]}],
            }
        ],
    )
    with pytest.raises(ProjectIntegrityError, match="deployment 'productie' component 'web'"):
        await validate_project_structure(data)
