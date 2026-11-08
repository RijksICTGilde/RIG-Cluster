"""publish-on-web on a component without an inbound port is refused, in every layout.

Found by ``test_add_service_provisions_every_dependency`` in tasks_live: a component added
without a port (a background worker, which the API allows) got publish-on-web, and the
default layout rendered an ingress with ``port.number: None`` that ArgoCD refused for the
whole Application. The check existed for the dotted layouts only.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.manager.project_manager import ProjectManager


def _project(inbound: list[int]) -> dict:
    return {
        "name": "demo",
        "components": [
            {"name": "web", "type": "single", "ports": {"inbound": inbound}, "services": ["publish-on-web"]}
        ],
        "deployments": [
            {
                "name": "main",
                "cluster": "sandboxed-local",
                "namespace": "demo",
                "components": [{"reference": "web", "image": "nginx:1"}],
            }
        ],
    }


async def test_a_component_without_a_port_cannot_be_published(tmp_path) -> None:
    project = _project([])
    with patch("opi.manager.project_manager.KubectlConnector"):
        pm = ProjectManager()
    pm.get_contents = AsyncMock(return_value=project)
    pm.get_name = AsyncMock(return_value="demo")
    git = MagicMock()
    git.get_working_dir = AsyncMock(return_value=str(tmp_path))

    with pytest.raises(ValueError, match="Missing ports for: web"):
        await pm.create_application_manifests(project["deployments"][0], git, "uit")
