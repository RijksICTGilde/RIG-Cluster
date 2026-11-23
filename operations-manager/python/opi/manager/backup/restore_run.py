"""Restoring a backup into a deployment: the one path every entry point runs.

The restore routes, the modal wizard and the task handler each ran their own version of the
same steps. :func:`restore_deployment` is that handling for an existing deployment; the
primitives it drives live in :mod:`opi.manager.backup.restore`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

from opi.core.backup_constants import ResourceType
from opi.core.cluster_config import get_storage_access_modes, get_storage_class_name
from opi.manager.backup.base import BackupLockBusyError
from opi.manager.backup.pvc_backup import create_backup_manager
from opi.manager.backup.restore import (
    GENERATION_NOT_RECORDED,
    TERUGZETTEN_MISLUKT,
    GenerationUpdate,
    restore_bucket_with_versioning,
    restore_database_with_versioning,
    restore_pvc_with_versioning,
    set_restored_generation,
)
from opi.manager.backup.run import ProjectNotFoundError, resolve_deployment_target
from opi.manager.project_manager import ProjectManager
from opi.services.catalog.shared.storage_size import effective_persistent_size_by_name
from opi.services.clone_validation import build_restore_target_deployment
from opi.services.project_file_handler import create_project_file_handler, find_persistent_storage
from opi.services.project_store import get_project_store
from opi.services.schema_migration import normalize_service_entries
from opi.services.tasks import progress_steps as steps
from opi.utils.naming import generate_pvc_name, generate_unique_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opi.manager.backup.run import ProgressSink

logger = logging.getLogger(__name__)

#: What one restore step may break on without taking the steps behind it down.
STEP_ERRORS = (ValueError, RuntimeError, KeyError, OSError, ConnectionError)


class RefreshPolicy(StrEnum):
    """Whether the deployment is regenerated once its data is back."""

    #: Regenerate as soon as at least one item landed: the project file then points at a
    #: generation the running deployment does not use yet.
    ON_ANY_SUCCESS = "on_any_success"
    NEVER = "never"


class DeploymentRefresh(Protocol):
    """Regenerating the manifests and secrets of one deployment; the reason it failed, or None.

    A ``Protocol`` so the use case does not reach into ``opi/runtime`` for the progress
    manager the provisioning reports its steps to.
    """

    async def __call__(self, project_file_path: str, deployment_name: str) -> str | None: ...


@dataclass(frozen=True)
class RestoreItem:
    """One resource a restore puts back, named as the backup registered it."""

    resource_type: ResourceType
    component_name: str
    reference_name: str
    snapshot_id: str
    #: The generation of the resource this snapshot came from. Only a restore into a NEW
    #: deployment needs it, to name that resource.
    source_generation: int = 0

    @property
    def label(self) -> str:
        """What the progress shows for this item."""
        return f"{self.resource_type.value.upper()}: {self.component_name or self.reference_name}"


def restore_item_from_backup_item(item: Mapping[str, Any]) -> RestoreItem:
    """A backup-run item, in the shape ``group_snapshots_into_runs`` writes, as a restore item."""
    return RestoreItem(
        resource_type=ResourceType.from_string(item.get("resource_type")),
        component_name=str(item.get("component_name") or ""),
        reference_name=str(item.get("storage_name") or item.get("reference_name") or ""),
        snapshot_id=str(item.get("snapshot_id") or ""),
        source_generation=int(item.get("generation") or 0),
    )


@dataclass(frozen=True)
class RestoreRequest:
    """One restore run: which deployment gets which items back, and what follows it.

    The target comes resolved, through :func:`opi.manager.backup.run.resolve_deployment_target`,
    so a deployment without a cluster or on another cluster is refused before anything is
    restored.
    """

    project_name: str
    target_deployment: str
    items: list[RestoreItem]
    project_file_path: str
    project_data: dict[str, Any]
    namespace: str
    cluster: str
    refresh: RefreshPolicy = RefreshPolicy.ON_ANY_SUCCESS
    #: Names the run in the commit message when the items came from one.
    backup_run_id: str = ""


@dataclass(frozen=True)
class RestoredItem:
    """What one item of a restore produced, whether it landed or not."""

    item: RestoreItem
    success: bool
    error: str | None = None
    old_generation: int = 0
    new_generation: int = 0
    old_resource_name: str = ""
    new_resource_name: str = ""
    #: Whether this item gave up waiting for the backup lock.
    lock_busy: bool = False


@dataclass
class RestoreOutcome:
    """What one restore run produced, per item, and what followed it."""

    items: list[RestoredItem] = field(default_factory=list)
    project_updated: bool = False
    refresh_triggered: bool = False
    #: What the backup lock said when an item gave up waiting for it.
    lock_error: str | None = None
    #: Why the refresh failed; None when it succeeded or was never triggered.
    refresh_error: str | None = None

    @property
    def refresh_succeeded(self) -> bool | None:
        """Whether the refresh landed. None when none was triggered."""
        return None if not self.refresh_triggered else self.refresh_error is None

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def succeeded(self) -> int:
        return sum(1 for restored in self.items if restored.success)

    @property
    def all_restored(self) -> bool:
        """Whether every item landed. True for a request without items: nothing failed."""
        return self.succeeded == self.total


async def restore_deployment(
    request: RestoreRequest,
    progress: ProgressSink,
    parent_task_id: str,
    refresh: DeploymentRefresh,
) -> RestoreOutcome:
    """Put every item back into its deployment, each into a new generation.

    Every item gets its own subtask, its own generation commit and its own entry in the
    outcome; an item that breaks does not stop the ones behind it. The deployment is
    regenerated afterwards when the request's policy asks for it.
    """
    outcome = RestoreOutcome()
    for item in request.items:
        subtask = progress.add_subtask(parent_task_id, steps.RESTORE_ITEM, subject=item.label)
        restored = await _restore_and_record(request, item, outcome)
        outcome.items.append(restored)
        if restored.lock_busy:
            outcome.lock_error = restored.error
        if restored.success:
            progress.complete_subtask(subtask)
        else:
            progress.fail_subtask(subtask, restored.error or TERUGZETTEN_MISLUKT)

    await _refresh_deployment(request, outcome, refresh)
    return outcome


async def _restore_and_record(
    request: RestoreRequest,
    item: RestoreItem,
    outcome: RestoreOutcome,
) -> RestoredItem:
    """Restore one item and record its generation; the item as it ended.

    A generation that cannot be committed makes the item a failure: its data is in the new
    resource while the project file still points at the old one, and the caller has to see
    that.
    """
    restored = await _restore_item(request, item)
    if not restored.success:
        return restored

    try:
        await _record_generation(request, item, restored.new_generation)
    except STEP_ERRORS:
        logger.exception(
            "Generation %s of %s could not be recorded for %s/%s",
            restored.new_generation,
            item.label,
            request.project_name,
            request.target_deployment,
        )
        return replace(restored, success=False, error=GENERATION_NOT_RECORDED)

    outcome.project_updated = True
    return restored


async def _restore_item(request: RestoreRequest, item: RestoreItem) -> RestoredItem:
    """Run the versioning restore of one item; what it produced."""
    if not item.reference_name:
        # Without a reference there is nothing to look up at Kopia and nothing to name the
        # new generation after. A component is NOT required: a database or bucket of a
        # deployment whose components do not carry the service has none.
        logger.warning("Skipping snapshot %s: it carries no reference name", item.snapshot_id)
        return RestoredItem(
            item=item, success=False, error=f"Missing reference name in {item.resource_type.value} snapshot"
        )

    try:
        result = await _restore_with_versioning(request, item)
    except STEP_ERRORS as e:
        logger.exception("Restore of %s failed for %s/%s", item.label, request.project_name, request.target_deployment)
        # The exception names the restore pod and the tail of its log, which belongs in
        # the log and not in an answer the caller reads.
        lock_busy = isinstance(e, BackupLockBusyError)
        return RestoredItem(
            item=item, success=False, error=str(e) if lock_busy else TERUGZETTEN_MISLUKT, lock_busy=lock_busy
        )

    restored = RestoredItem(
        item=item,
        success=bool(result["success"]),
        error=result.get("error"),
        old_generation=result.get("old_generation") or 0,
        new_generation=result.get("new_generation") or 0,
        old_resource_name=result.get("old_resource_name") or "",
        new_resource_name=result.get("new_resource_name") or "",
    )
    logger.info(
        "Restored %s for %s/%s: %s -> %s (gen %s -> %s, success %s)",
        item.resource_type.value,
        request.project_name,
        request.target_deployment,
        restored.old_resource_name,
        restored.new_resource_name,
        restored.old_generation,
        restored.new_generation,
        restored.success,
    )
    return restored


async def _restore_with_versioning(request: RestoreRequest, item: RestoreItem) -> dict[str, Any]:
    """Dispatch to the primitive of this resource type."""
    shared: dict[str, Any] = {
        "project_name": request.project_name,
        "deployment_name": request.target_deployment,
        "component_name": item.component_name,
        "snapshot_id": item.snapshot_id,
        "deployment_cluster": request.cluster,
        "namespace": request.namespace,
        "project_data": request.project_data,
        "project_file_handler": create_project_file_handler(),
    }
    match item.resource_type:
        case ResourceType.PVC:
            return await restore_pvc_with_versioning(storage_name=item.reference_name, **shared)
        case ResourceType.DATABASE:
            return await restore_database_with_versioning(reference_name=item.reference_name, **shared)
        case ResourceType.BUCKET:
            return await restore_bucket_with_versioning(reference_name=item.reference_name, **shared)


async def _record_generation(request: RestoreRequest, item: RestoreItem, new_generation: int) -> None:
    """Write the new generation into the project file and commit it.

    Read fresh from git so the write merges onto what landed in between, and saved with
    validation off: the data is already in the new generation, so refusing to record it
    would leave the file pointing at the old resource with no way back.
    """
    project_manager = ProjectManager(project_file_relative_path=request.project_file_path)
    try:
        project_data = await project_manager.get_contents()
        set_restored_generation(
            create_project_file_handler(),
            project_data,
            GenerationUpdate(
                deployment=request.target_deployment,
                component=item.component_name,
                reference=item.reference_name,
                resource_type=item.resource_type,
                new_generation=new_generation,
            ),
        )
        await project_manager.save_and_commit_project(
            project_data,
            _generation_commit_message(request, item, new_generation),
            enforce_validation=False,
        )
    finally:
        await project_manager.close()


def _generation_commit_message(request: RestoreRequest, item: RestoreItem, new_generation: int) -> str:
    run = f" from run {request.backup_run_id}" if request.backup_run_id else ""
    return (
        f"Restore {item.resource_type.value} for {request.project_name}/{request.target_deployment}"
        f"{run} (gen {new_generation})"
    )


async def _refresh_deployment(
    request: RestoreRequest,
    outcome: RestoreOutcome,
    refresh: DeploymentRefresh,
) -> None:
    """Regenerate the deployment when the policy asks for it, and record how that went."""
    if request.refresh is RefreshPolicy.NEVER or not outcome.project_updated:
        return
    outcome.refresh_triggered = True
    outcome.refresh_error = await refresh(request.project_file_path, request.target_deployment)
    if outcome.refresh_error:
        logger.error(
            "Restore of %s/%s landed but the deployment was not updated to use it: %s",
            request.project_name,
            request.target_deployment,
            outcome.refresh_error,
        )


def restore_outcome_answer(outcome: RestoreOutcome) -> dict[str, Any]:
    """The outcome as a task answer: what ``handle_restore`` returns and the API reads back.

    ``success: False`` is what the worker reads to fail the task, and the per-item outcome
    stays on the answer either way, so a partial restore still says what came back.
    """
    answer: dict[str, Any] = {
        "success": outcome.all_restored and outcome.refresh_succeeded is not False,
        "total_items": outcome.total,
        "restored_items": [
            {
                "resource_type": restored.item.resource_type.value,
                "component_name": restored.item.component_name,
                "reference_name": restored.item.reference_name,
                "snapshot_id": restored.item.snapshot_id,
                "success": restored.success,
                "error": restored.error,
                "old_generation": restored.old_generation,
                "new_generation": restored.new_generation,
                "old_resource_name": restored.old_resource_name,
                "new_resource_name": restored.new_resource_name,
            }
            for restored in outcome.items
        ],
        "project_updated": outcome.project_updated,
        "refresh_triggered": outcome.refresh_triggered,
        "refresh_succeeded": outcome.refresh_succeeded,
        "lock_error": outcome.lock_error,
    }
    if not answer["success"]:
        answer["error"] = outcome.refresh_error or "Herstel gedeeltelijk mislukt"
    return answer


@dataclass(frozen=True)
class NewDeploymentRestore:
    """A restore into a deployment that does not exist yet: what it is built from."""

    project_name: str
    target_deployment: str
    source_deployment: str
    backup_run_id: str
    #: The deployment config the wizard collected, or None for an empty deployment.
    deployment_config: dict[str, Any] | None = None
    #: The deployment whose images and env vars the new one inherits, None for none.
    config_source: str | None = None


async def create_deployment_for_restore(request: NewDeploymentRestore) -> None:
    """Add the restore-target deployment to the project file and commit it.

    Saved here and not by the modal wizard: :func:`pre_restore_pvcs` has to fill the PVCs
    before ArgoCD syncs the project, and a save from the wizard starts the processing right
    away.

    Raises:
        ProjectNotFoundError: when the project does not exist or carries no data.
    """
    project = get_project_store().get(request.project_name)
    if not project or not project.data:
        raise ProjectNotFoundError(f"Project '{request.project_name}' niet gevonden")

    # Read fresh from Git (not the read-only cache) so the mutation merges onto current
    # state and persists through the single validated save path. Closed explicitly so its
    # temp git clone is cleaned up.
    project_manager = ProjectManager(project_file_relative_path=f"projects/{project.filename}")
    try:
        project_data = await project_manager.get_contents()
        new_deployment = build_restore_target_deployment(
            project_data,
            target_deployment=request.target_deployment,
            backup_source=request.source_deployment,
            backup_run_id=request.backup_run_id,
            config_source=request.config_source,
            wizard_config=request.deployment_config,
        )
        project_data.setdefault("deployments", []).append(new_deployment)
        # The wizard editables write service config in the name-as-key shape, which the
        # schema's deployment-service envelope rejects; the other wizard save paths
        # normalize too.
        normalize_service_entries(project_data)
        await project_manager.save_and_commit_project(
            project_data,
            f"Add deployment '{request.target_deployment}' to project '{request.project_name}' "
            f"(restore from '{request.source_deployment}')",
        )
    finally:
        await project_manager.close()

    logger.info(
        "Created deployment '%s' in project '%s' (restore from '%s', config from '%s')",
        request.target_deployment,
        request.project_name,
        request.source_deployment,
        request.config_source or "(leeg)",
    )


async def pre_restore_pvcs(
    request: NewDeploymentRestore,
    items: list[RestoreItem],
    progress: ProgressSink,
    parent_task_id: str,
) -> None:
    """Create the namespace and fill the PVCs of the new deployment with the backup data.

    Before the infrastructure is provisioned: when ArgoCD later syncs the generated
    manifests it adopts the PVCs that are already there, and already filled, through
    ``Replace=false``.

    Raises:
        DeploymentLookupError: when the new deployment cannot be resolved.
        RuntimeError: when a PVC did not come back; the ones before it stay.
    """
    project, project_data, app_namespace, cluster = await resolve_deployment_target(
        request.project_name, request.target_deployment
    )

    project_manager = ProjectManager(project_file_relative_path=f"projects/{project.filename}")
    try:
        await project_manager.check_and_create_namespaces(request.target_deployment)
    finally:
        await project_manager.close()

    base_components = {c.get("name"): c for c in project_data.get("components", [])}
    backup_manager = create_backup_manager()
    storage_class = get_storage_class_name(cluster)
    access_modes = get_storage_access_modes(cluster)

    for item in items:
        subtask = progress.add_subtask(
            parent_task_id, steps.RESTORE_ITEM, subject=f"PVC: {item.component_name}/{item.reference_name}"
        )

        # The source PVC carries the name of the deployment the backup was tagged with, the
        # target the generation a fresh deployment starts on.
        source_pvc_name = generate_pvc_name(
            generate_unique_name(request.source_deployment, item.component_name),
            item.reference_name,
            item.source_generation,
        )
        target_pvc_name = generate_pvc_name(
            generate_unique_name(request.target_deployment, item.component_name), item.reference_name, 0
        )

        target_storage = find_persistent_storage(base_components.get(item.component_name, {}), item.reference_name)
        result = await backup_manager.restore_to_project_pvc(
            cluster=cluster,
            namespace=app_namespace,
            source_pvc_name=source_pvc_name,
            target_pvc_name=target_pvc_name,
            # The effective size of the TARGET deployment's volume; see restore.py.
            storage_size=effective_persistent_size_by_name(
                project_data, request.target_deployment, item.component_name, item.reference_name
            ),
            storage_class=storage_class,
            access_modes=access_modes,
            snapshot_id=item.snapshot_id,
            backup_enabled=target_storage.get("backup", True) if target_storage else True,
            project_name=request.project_name,
        )

        if not result.success:
            logger.error("Pre-restore of PVC %s failed: %s", target_pvc_name, result.error)
            progress.fail_subtask(subtask, TERUGZETTEN_MISLUKT)
            raise RuntimeError(TERUGZETTEN_MISLUKT)

        progress.complete_subtask(subtask)
        logger.info("Pre-restored PVC %s -> %s in %s", source_pvc_name, target_pvc_name, app_namespace)
