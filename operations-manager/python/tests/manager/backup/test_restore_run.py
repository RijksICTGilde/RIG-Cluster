"""The restore use case: the steps it takes per item, and what it does when one fails.

Every entry point runs ``restore_deployment`` (RC-482), so what is pinned here holds for
the API routes, the modal wizard and the task handler alike:

* one item that fails does not stop the ones behind it, and the outcome says which is which;
* the generation of an item is committed right after that item came back, so a half-finished
  run leaves a project file that matches the cluster;
* a generation that cannot be committed makes the item a FAILURE, because its data is in the
  new resource while the file still points at the old one;
* the refresh runs as soon as something came back, and not at all when nothing did.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opi.core.backup_constants import ResourceType
from opi.manager.backup.base import BackupLockBusyError
from opi.manager.backup.restore import GENERATION_NOT_RECORDED, TERUGZETTEN_MISLUKT
from opi.manager.backup.restore_run import (
    RefreshPolicy,
    RestoreItem,
    RestoreRequest,
    restore_deployment,
    restore_item_from_backup_item,
    restore_outcome_answer,
)

PROJECT = "demo"
DEPLOYMENT = "prod"


def _request(items: list[RestoreItem], refresh: RefreshPolicy = RefreshPolicy.ON_ANY_SUCCESS) -> RestoreRequest:
    return RestoreRequest(
        project_name=PROJECT,
        target_deployment=DEPLOYMENT,
        items=items,
        project_file_path=f"projects/{PROJECT}.yaml",
        project_data={"name": PROJECT, "deployments": [{"name": DEPLOYMENT}]},
        namespace=f"rig-{PROJECT}",
        cluster="local",
        refresh=refresh,
    )


def _item(resource_type: ResourceType = ResourceType.PVC, reference: str = "data") -> RestoreItem:
    return RestoreItem(
        resource_type=resource_type,
        component_name="web",
        reference_name=reference,
        snapshot_id=f"snap-{reference}",
    )


def _restored(**overrides: Any) -> dict[str, Any]:
    return {
        "success": True,
        "error": None,
        "old_generation": 0,
        "new_generation": 1,
        "old_resource_name": "resource-v0",
        "new_resource_name": "resource-v1",
    } | overrides


@pytest.fixture
def progress() -> MagicMock:
    progress = MagicMock()
    progress.add_subtask = MagicMock(side_effect=lambda parent, name, subject=None: f"sub-{subject}")
    return progress


@pytest.fixture
def refresh() -> AsyncMock:
    """The refresh collaborator: None means it landed."""
    return AsyncMock(return_value=None)


@pytest.fixture
def project_manager() -> Any:
    """The ProjectManager the generation commit runs through."""
    manager = MagicMock()
    manager.get_contents = AsyncMock(return_value={"name": PROJECT, "deployments": [{"name": DEPLOYMENT}]})
    manager.save_and_commit_project = AsyncMock()
    manager.close = AsyncMock()
    with patch("opi.manager.backup.restore_run.ProjectManager", return_value=manager):
        yield manager


@pytest.fixture
def primitives() -> Any:
    """The three versioning restores, each landing by default."""
    with (
        patch("opi.manager.backup.restore_run.restore_pvc_with_versioning") as pvc,
        patch("opi.manager.backup.restore_run.restore_database_with_versioning") as database,
        patch("opi.manager.backup.restore_run.restore_bucket_with_versioning") as bucket,
    ):
        for primitive in (pvc, database, bucket):
            primitive.return_value = _restored()
        yield {"pvc": pvc, "database": database, "bucket": bucket}


@pytest.mark.asyncio
@pytest.mark.usefixtures("project_manager")
class TestEveryItemGetsItsOwnOutcome:
    async def test_each_item_runs_the_primitive_of_its_type(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock
    ) -> None:
        request = _request([_item(), _item(ResourceType.DATABASE, "db"), _item(ResourceType.BUCKET, "bucket")])

        outcome = await restore_deployment(request, progress, "task-1", refresh)

        assert outcome.all_restored is True
        assert outcome.total == 3
        primitives["pvc"].assert_awaited_once()
        primitives["database"].assert_awaited_once()
        primitives["bucket"].assert_awaited_once()

    async def test_a_failing_item_does_not_stop_the_next_one(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock
    ) -> None:
        primitives["pvc"].return_value = _restored(success=False, error=TERUGZETTEN_MISLUKT)
        request = _request([_item(), _item(ResourceType.DATABASE, "db")])

        outcome = await restore_deployment(request, progress, "task-1", refresh)

        assert [restored.success for restored in outcome.items] == [False, True]
        assert outcome.succeeded == 1
        assert outcome.all_restored is False
        primitives["database"].assert_awaited_once()

    async def test_an_exception_does_not_carry_the_pod_log_into_the_outcome(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock
    ) -> None:
        primitives["pvc"].side_effect = OSError("Connect call failed ('10.0.0.1', 5432)")

        outcome = await restore_deployment(_request([_item()]), progress, "task-1", refresh)

        assert outcome.items[0].success is False
        assert outcome.items[0].error == TERUGZETTEN_MISLUKT

    async def test_a_held_backup_lock_is_marked_on_the_outcome(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock
    ) -> None:
        """So an entry point can answer 409 for it without matching on the message."""
        primitives["pvc"].side_effect = BackupLockBusyError("Could not acquire backup lock")

        outcome = await restore_deployment(_request([_item()]), progress, "task-1", refresh)

        assert outcome.lock_error == "Could not acquire backup lock"
        assert outcome.items[0].error == "Could not acquire backup lock"

    async def test_an_item_without_a_reference_is_refused_before_the_primitive(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock
    ) -> None:
        request = _request([_item(reference="")])

        outcome = await restore_deployment(request, progress, "task-1", refresh)

        assert outcome.items[0].success is False
        assert "Missing reference name" in str(outcome.items[0].error)
        primitives["pvc"].assert_not_awaited()


@pytest.mark.asyncio
class TestTheGenerationIsCommittedPerItem:
    async def test_the_commit_happens_per_item_with_validation_off(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock, project_manager: Any
    ) -> None:
        """Validation off: the data is already in the new generation, so refusing to record
        it would leave the file pointing at the old resource with no way back."""
        request = _request([_item(), _item(ResourceType.DATABASE, "db")])

        await restore_deployment(request, progress, "task-1", refresh)

        assert project_manager.save_and_commit_project.await_count == 2
        for call in project_manager.save_and_commit_project.await_args_list:
            assert call.kwargs["enforce_validation"] is False

    async def test_a_failing_item_is_not_committed(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock, project_manager: Any
    ) -> None:
        primitives["pvc"].return_value = _restored(success=False, error=TERUGZETTEN_MISLUKT)

        outcome = await restore_deployment(_request([_item()]), progress, "task-1", refresh)

        project_manager.save_and_commit_project.assert_not_awaited()
        assert outcome.project_updated is False

    async def test_a_generation_that_cannot_be_committed_makes_the_item_fail(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock, project_manager: Any
    ) -> None:
        project_manager.save_and_commit_project = AsyncMock(side_effect=RuntimeError("push geweigerd"))

        outcome = await restore_deployment(_request([_item()]), progress, "task-1", refresh)

        assert outcome.items[0].success is False
        assert outcome.items[0].error == GENERATION_NOT_RECORDED
        assert outcome.project_updated is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("project_manager")
class TestWhenTheDeploymentIsRegenerated:
    async def test_one_item_that_landed_is_enough(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock
    ) -> None:
        """The project file points at the new generation of that one, so the deployment has
        to be switched over to it even though the rest failed."""
        primitives["database"].return_value = _restored(success=False, error=TERUGZETTEN_MISLUKT)
        request = _request([_item(), _item(ResourceType.DATABASE, "db")])

        outcome = await restore_deployment(request, progress, "task-1", refresh)

        refresh.assert_awaited_once_with(f"projects/{PROJECT}.yaml", DEPLOYMENT)
        assert outcome.refresh_triggered is True
        assert outcome.refresh_succeeded is True

    async def test_nothing_that_landed_means_no_refresh(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock
    ) -> None:
        primitives["pvc"].return_value = _restored(success=False, error=TERUGZETTEN_MISLUKT)

        outcome = await restore_deployment(_request([_item()]), progress, "task-1", refresh)

        refresh.assert_not_awaited()
        assert outcome.refresh_triggered is False
        assert outcome.refresh_succeeded is None

    async def test_the_never_policy_leaves_the_deployment_alone(
        self, primitives: Any, progress: MagicMock, refresh: AsyncMock
    ) -> None:
        request = _request([_item()], refresh=RefreshPolicy.NEVER)

        outcome = await restore_deployment(request, progress, "task-1", refresh)

        refresh.assert_not_awaited()
        assert outcome.refresh_triggered is False

    async def test_a_failed_refresh_is_on_the_outcome_and_in_the_answer(
        self, primitives: Any, progress: MagicMock
    ) -> None:
        refresh = AsyncMock(return_value="manifesten konden niet worden gegenereerd")

        outcome = await restore_deployment(_request([_item()]), progress, "task-1", refresh)

        assert outcome.all_restored is True
        assert outcome.refresh_succeeded is False
        answer = restore_outcome_answer(outcome)
        assert answer["success"] is False
        assert answer["error"] == "manifesten konden niet worden gegenereerd"


class TestABackupRunItemBecomesARestoreItem:
    def test_a_pvc_item_carries_its_storage_name_and_source_generation(self) -> None:
        item = restore_item_from_backup_item(
            {
                "resource_type": "pvc",
                "component_name": "web",
                "storage_name": "data",
                "reference_name": "data",
                "snapshot_id": "snap-1",
                "generation": 3,
            }
        )

        assert item == RestoreItem(
            resource_type=ResourceType.PVC,
            component_name="web",
            reference_name="data",
            snapshot_id="snap-1",
            source_generation=3,
        )

    def test_a_bucket_item_keeps_the_bucket_type(self) -> None:
        """The snapshot tag says ``bucket`` where the API says ``minio``."""
        item = restore_item_from_backup_item(
            {"resource_type": "bucket", "reference_name": "prod-minio", "snapshot_id": "snap-2"}
        )

        assert item.resource_type is ResourceType.BUCKET
        assert item.reference_name == "prod-minio"
        assert item.source_generation == 0
