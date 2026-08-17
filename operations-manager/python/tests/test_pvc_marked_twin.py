"""Tests for reconciling marked-for-deletion PVC state when a PVC is recreated.

Covers Bevinding A of the RC-6 plan. When a persistent-storage service is removed the PVC
manifest is renamed to ``<base>.marked-for-deletion.yaml`` so ArgoCD keeps the volume alive.
If the same storage is later re-added, OPI must:

1. remove the marked twin file before writing ``<base>.yaml`` again, otherwise two files
   carry the same PVC identity and kustomize refuses to render the whole deployment;
2. drop the now-stale ``marked_for_deletion`` row - the file is the source of truth, so a
   mark whose file no longer exists is deleted (selected on project + deployment + type).
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.generation.manifests import ManifestGenerator
from opi.manager.pvc_manager import MARKED_FOR_DELETION_SUFFIX, PVCManager


def _make_pvc_manager(generation: int = 0, backup_enabled: bool = False) -> PVCManager:
    """Build a PVCManager with a minimally-mocked ProjectManager.

    Only the project-file handler is exercised for the no-clone path used here.
    """
    project_manager = MagicMock()
    project_manager._project_file_handler.get_storage_generation.return_value = generation
    project_manager._project_file_handler.get_storage_backup_enabled.return_value = backup_enabled
    return PVCManager(project_manager)


def _mark(resource_name: str, mark_id: str = "m1") -> dict:
    return {"id": mark_id, "resource_type": "pvc", "resource_name": resource_name}


async def _create(mgr: PVCManager, out: str, marks: list[dict], delete_mark: AsyncMock) -> list[str]:
    """Run create_pvc_manifests_for_component with a mocked marked-for-deletion service."""
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
async def test_recreate_removes_marked_twin_and_deletes_stale_mark(tmp_path):
    out = str(tmp_path)
    marked_name = f"webapp-data-pvc{MARKED_FOR_DELETION_SUFFIX}"
    marked = os.path.join(out, marked_name)
    with open(marked, "w") as f:
        f.write("kind: PersistentVolumeClaim\n")

    delete_mark = AsyncMock(return_value=True)
    created = await _create(_make_pvc_manager(generation=0), out, [_mark(marked_name)], delete_mark)

    # The plain manifest is (re)written and the marked twin is gone.
    assert created == ["webapp-data-pvc.yaml"]
    assert os.path.exists(os.path.join(out, "webapp-data-pvc.yaml"))
    assert not os.path.exists(marked)
    # Its row is stale now (file gone) and gets deleted by id.
    delete_mark.assert_awaited_once_with("m1")


@pytest.mark.asyncio
async def test_recreate_keeps_still_marked_sibling(tmp_path):
    out = str(tmp_path)
    # The recreated storage's twin...
    recreated_twin = os.path.join(out, f"webapp-data-pvc{MARKED_FOR_DELETION_SUFFIX}")
    open(recreated_twin, "w").close()
    # ...and a genuinely-still-marked sibling storage whose file remains on disk.
    sibling_name = f"webapp-cache-pvc{MARKED_FOR_DELETION_SUFFIX}"
    sibling = os.path.join(out, sibling_name)
    open(sibling, "w").close()

    delete_mark = AsyncMock(return_value=True)
    marks = [_mark(f"webapp-data-pvc{MARKED_FOR_DELETION_SUFFIX}", "m1"), _mark(sibling_name, "m2")]
    await _create(_make_pvc_manager(generation=0), out, marks, delete_mark)

    # Only the recreated storage's stale mark is deleted; the sibling (file present) stays.
    assert not os.path.exists(recreated_twin)
    assert os.path.exists(sibling)
    delete_mark.assert_awaited_once_with("m1")


@pytest.mark.asyncio
async def test_recreate_keeps_marked_twin_of_other_generation(tmp_path):
    out = str(tmp_path)
    # A marked twin left behind for generation 0.
    gen0_marked = os.path.join(out, f"webapp-data-pvc{MARKED_FOR_DELETION_SUFFIX}")
    open(gen0_marked, "w").close()

    # Recreating generation 2: its twin would be webapp-data-pvc-v2.marked-for-deletion.yaml.
    delete_mark = AsyncMock(return_value=True)
    created = await _create(_make_pvc_manager(generation=2), out, [], delete_mark)

    assert created == ["webapp-data-pvc-v2.yaml"]
    # The generation-0 marked twin is not this generation's twin, so the file must remain.
    assert os.path.exists(gen0_marked)


def test_remove_marked_twin_returns_true_when_removed(tmp_path):
    out = str(tmp_path)
    marked = os.path.join(out, f"webapp-data-pvc{MARKED_FOR_DELETION_SUFFIX}")
    open(marked, "w").close()

    mgr = _make_pvc_manager()
    assert mgr._remove_marked_twin(out, "webapp-data-pvc.yaml") is True
    assert not os.path.exists(marked)


def test_remove_marked_twin_noop_when_no_twin(tmp_path):
    mgr = _make_pvc_manager()
    assert mgr._remove_marked_twin(str(tmp_path), "webapp-data-pvc.yaml") is False


@pytest.mark.asyncio
async def test_prune_stale_pvc_marks_without_db_is_a_warning(tmp_path):
    mgr = _make_pvc_manager()
    with (
        patch("opi.core.database_pools.get_database_pool", side_effect=KeyError("main")),
        patch("opi.services.marked_for_deletion_service.MarkedForDeletionService") as svc,
    ):
        await mgr._prune_stale_pvc_marks(str(tmp_path), "proj", "deploy-a", "local")
    # No DB -> the service is never constructed; the file removal already fixed the render.
    svc.assert_not_called()


@pytest.mark.asyncio
async def test_prune_stale_pvc_marks_only_deletes_missing_files(tmp_path):
    out = str(tmp_path)
    present_name = f"webapp-keep-pvc{MARKED_FOR_DELETION_SUFFIX}"
    open(os.path.join(out, present_name), "w").close()
    missing_name = f"webapp-gone-pvc{MARKED_FOR_DELETION_SUFFIX}"

    delete_mark = AsyncMock(return_value=True)
    mgr = _make_pvc_manager()
    with (
        patch("opi.core.database_pools.get_database_pool"),
        patch("opi.services.marked_for_deletion_service.MarkedForDeletionService") as svc,
    ):
        svc.return_value.get_marks_for_deployment = AsyncMock(
            return_value=[_mark(present_name, "keep"), _mark(missing_name, "gone")]
        )
        svc.return_value.delete_mark = delete_mark
        await mgr._prune_stale_pvc_marks(out, "proj", "deploy-a", "local")

    delete_mark.assert_awaited_once_with("gone")
