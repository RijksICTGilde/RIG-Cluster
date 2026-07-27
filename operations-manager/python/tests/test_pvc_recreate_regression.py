"""Regression test replaying the RC-6 incident end to end.

A persistent-storage service is removed (PVC manifest renamed to the marked twin, mark
recorded), then the same storage is re-added. The deployment directory must end with exactly
one PVC manifest, the duplicate-identity failsafe must not fire, and kustomization.yaml must
list the PVC once.
"""

import glob
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.generation.manifests import ManifestGenerator, check_duplicate_resource_identities
from opi.manager.pvc_manager import MARKED_FOR_DELETION_SUFFIX, PVCManager

MANIFESTS_DIR = os.path.join(os.path.dirname(__file__), "..", "manifests")


def _make_pvc_manager() -> PVCManager:
    project_manager = MagicMock()
    project_manager._project_file_handler.get_storage_generation.return_value = 0
    project_manager._project_file_handler.get_storage_backup_enabled.return_value = False
    return PVCManager(project_manager)


async def _create(mgr: PVCManager, out: str, marks: list[dict], delete_mark: AsyncMock) -> list[str]:
    with (
        patch("opi.core.database_pools.get_database_pool"),
        patch("opi.services.marked_for_deletion_service.MarkedForDeletionService") as svc,
    ):
        svc.return_value.get_marks_for_deployment = AsyncMock(return_value=marks)
        svc.return_value.delete_mark = delete_mark
        return await mgr.create_pvc_manifests_for_component(
            project_data={"name": "proj"},
            deployment={"name": "deploy-a"},
            component_name="webapp",
            unique_name="deploy-a-webapp",
            persistent_storage=[{"name": "data", "size": "1Gi"}],
            namespace="proj",
            cluster="local",
            full_output_dir=out,
            manifest_generator=ManifestGenerator(),
        )


@pytest.mark.asyncio
async def test_remove_then_recreate_keeps_single_pvc_and_renders(tmp_path):
    out = str(tmp_path)
    mgr = _make_pvc_manager()
    plain = os.path.join(out, "webapp-data-pvc.yaml")
    marked = os.path.join(out, f"webapp-data-pvc{MARKED_FOR_DELETION_SUFFIX}")

    # 1. First deploy: the PVC manifest is generated.
    await _create(mgr, out, [], AsyncMock())
    assert os.path.exists(plain)

    # 2. Service removed: handle_service_removal renames it to the marked twin and records
    #    a mark row keyed by the marked filename.
    os.rename(plain, marked)
    marked_name = f"webapp-data-pvc{MARKED_FOR_DELETION_SUFFIX}"
    mark_row = {"id": "m1", "resource_type": "pvc", "resource_name": marked_name}
    assert os.path.exists(marked) and not os.path.exists(plain)

    # 3. Storage re-added: recreate. The marked twin must be removed and the stale mark
    #    (its file now gone) deleted.
    delete_mark = AsyncMock(return_value=True)
    await _create(mgr, out, [mark_row], delete_mark)

    assert os.path.exists(plain)
    assert not os.path.exists(marked)
    pvc_files = [os.path.basename(f) for f in glob.glob(os.path.join(out, "*pvc*.yaml"))]
    assert pvc_files == ["webapp-data-pvc.yaml"]
    delete_mark.assert_awaited_once_with("m1")

    # 4. The duplicate-identity failsafe does not fire, and kustomization.yaml lists the
    #    PVC exactly once.
    generator = ManifestGenerator()
    sops_files, regular_files = generator.collect_manifest_files(out, include_subfolders=False)
    check_duplicate_resource_identities(out, regular_files)  # must not raise

    with patch("opi.generation.manifests.settings") as mock_settings:
        mock_settings.MANIFESTS_PATH = MANIFESTS_DIR
        assert generator.create_kustomization_files(output_dir=out, sops_files=sops_files, regular_files=regular_files)

    from opi.utils.yaml_util import load_yaml_from_path

    kustomization = load_yaml_from_path(os.path.join(out, "kustomization.yaml"))
    assert kustomization["resources"].count("webapp-data-pvc.yaml") == 1
    assert not any(name.endswith(MARKED_FOR_DELETION_SUFFIX) for name in kustomization["resources"])
