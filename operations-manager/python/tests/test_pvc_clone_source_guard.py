"""PVC clone-from.reference must be checked against the project (RC-361).

Unchecked, the reference feeds straight into the dataSource of the cloned PVC; a
reference naming a deployment that is not part of this project leaves the PVC
Pending with a dataSource that points nowhere.
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.generation.manifests import ManifestGenerator
from opi.manager.pvc_manager import PVCManager


def _make_pvc_manager() -> PVCManager:
    project_manager = MagicMock()
    project_manager._project_file_handler.get_storage_generation.return_value = 0
    project_manager._project_file_handler.get_storage_backup_enabled.return_value = False
    return PVCManager(project_manager)


async def _create(mgr: PVCManager, out: str, deployment: dict, project_data: dict) -> list[str]:
    with (
        patch("opi.core.database_pools.get_database_pool"),
        patch("opi.services.marked_for_deletion_service.MarkedForDeletionService") as svc,
    ):
        svc.return_value.get_marks_for_deployment = AsyncMock(return_value=[])
        svc.return_value.delete_mark = AsyncMock()
        return await mgr.create_pvc_manifests_for_component(
            project_data=project_data,
            deployment=deployment,
            component_name="webapp",
            unique_name="deploy-a-webapp",
            persistent_storage=[{"name": "data", "size": "1Gi"}],
            namespace="proj",
            cluster="local",
            full_output_dir=out,
            manifest_generator=ManifestGenerator(),
        )


@pytest.mark.asyncio
async def test_refuses_a_reference_outside_the_project(tmp_path):
    out = str(tmp_path)
    mgr = _make_pvc_manager()
    project_data = {"name": "proj", "deployments": [{"name": "deploy-a"}]}
    deployment = {
        "name": "deploy-a",
        "clone-from": {"type": "deployment", "reference": "ander-project-prod"},
    }

    with pytest.raises(ValueError, match="not found in project"):
        await _create(mgr, out, deployment, project_data)

    assert os.listdir(out) == []


@pytest.mark.asyncio
async def test_accepts_a_reference_to_a_deployment_of_this_project(tmp_path):
    out = str(tmp_path)
    mgr = _make_pvc_manager()
    project_data = {"name": "proj", "deployments": [{"name": "deploy-a"}, {"name": "deploy-b"}]}
    deployment = {
        "name": "deploy-b",
        "clone-from": {"type": "deployment", "reference": "deploy-a"},
    }

    created = await _create(mgr, out, deployment, project_data)

    assert created
    assert os.path.exists(os.path.join(out, "webapp-data-pvc.yaml"))
