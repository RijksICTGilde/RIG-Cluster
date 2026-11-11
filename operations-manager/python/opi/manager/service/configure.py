"""The one use case behind every write of a service's config block.

``upsert``, ``clear`` and ``patch`` are one change with three shapes: they validate the
same target, write through the same save path, and reconcile the same deployments. The
dispatch, the no-op decision and the rollout live here, so the v2 config routes, the task
handler and the portal cannot answer the same request differently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opi.manager.service.errors import InvalidProjectNameError, error_from_result
from opi.manager.service.rollout import RolloutRequest, roll_out
from opi.services.tasks import progress_steps as steps
from opi.utils.project_names import validate_project_name

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.manager.service.progress import ProgressReporter

logger = logging.getLogger(__name__)

#: What an entrance is told when the project name cannot be one.
INVALID_PROJECT_NAME = (
    "Invalid project name format. Must start with lowercase letter, "
    "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
)


class ConfigOperation(StrEnum):
    """What a config write does at its target layer."""

    UPSERT = "upsert"
    CLEAR = "clear"
    PATCH = "patch"


@dataclass(frozen=True)
class ServiceConfigChange:
    """One config write, whichever entrance asked for it.

    ``target`` is a ``ConfigLayer`` value. ``deployment_names`` is the rollout scope the
    entrance resolved with ``affected_deployments``; None means project-wide.
    """

    project_name: str
    service_name: str
    target: str
    operation: ConfigOperation
    config: dict[str, Any] | list[Any] | None = None
    add: list[Any] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    list_field: str | None = None
    component_name: str | None = None
    deployment_name: str | None = None
    rollout: bool = True
    deployment_names: list[str] | None = None


@dataclass(frozen=True)
class ServiceConfigResult:
    """What the write produced, in the fields every entrance reports on."""

    changed: bool
    counts: dict[str, Any]
    generated: dict[str, Any]
    approvals: list[Any]
    warnings: list[Any] | None
    processing: dict[str, Any]

    @property
    def succeeded(self) -> bool:
        return self.processing.get("status") != "failed"


async def configure_service(
    change: ServiceConfigChange, project_manager: ProjectManager, progress: ProgressReporter
) -> ServiceConfigResult:
    """Write ``change`` into the project file and roll it out to the deployments it touches.

    Raises a :class:`~opi.manager.service.errors.ServiceChangeError` when the change cannot
    be made; the entrance turns that into its own refusal.
    """
    name_task = progress.add_task(steps.PROJECT_NAME_CHECK)
    if not validate_project_name(change.project_name):
        progress.fail_task(name_task, INVALID_PROJECT_NAME)
        progress.fail_project(INVALID_PROJECT_NAME)
        raise InvalidProjectNameError(INVALID_PROJECT_NAME)
    progress.complete_task(name_task)

    write_task = progress.add_task(steps.SERVICE_CONFIG_SAVE, subject=f"{change.service_name} op {change.target}")
    result = await _write(change, project_manager)
    if not result["success"]:
        error = error_from_result(result, "Unknown error configuring service")
        progress.fail_task(write_task, str(error))
        progress.fail_project(str(error))
        raise error
    progress.complete_task(write_task)

    # What the store really committed. Absent means a writer that does not report it, and
    # then the rollout runs: a round too many costs time, a round too few leaves the
    # cluster behind the project file.
    changed = bool(result.get("changed", True))
    processing = await roll_out(
        RolloutRequest(
            project_name=change.project_name,
            deployment_names=change.deployment_names,
            changed=changed,
            requested=change.rollout,
        ),
        project_manager,
        progress,
    )
    return ServiceConfigResult(
        changed=changed,
        counts=_counts(change.operation, result),
        generated=result.get("generated", {}),
        approvals=result.get("approvals", []),
        warnings=result.get("warnings") or None,
        processing=processing,
    )


async def _write(change: ServiceConfigChange, project_manager: ProjectManager) -> dict[str, Any]:
    """Hand the change to the mutation group that owns its operation."""
    if change.operation is ConfigOperation.CLEAR:
        return await project_manager.clear_service_config(
            change.service_name,
            change.target,
            component_name=change.component_name,
            deployment_name=change.deployment_name,
        )
    if change.operation is ConfigOperation.PATCH:
        return await project_manager.patch_service_config_list(
            change.service_name,
            change.target,
            add=change.add,
            remove=change.remove,
            list_field=change.list_field,
            component_name=change.component_name,
            deployment_name=change.deployment_name,
        )
    if change.config is None:
        raise ValueError(f"Upsert van service '{change.service_name}' zonder config")
    return await project_manager.configure_service(
        change.service_name,
        change.target,
        change.config,
        component_name=change.component_name,
        deployment_name=change.deployment_name,
    )


def _counts(operation: ConfigOperation, result: dict[str, Any]) -> dict[str, Any]:
    """What changed, in the counters the operation reports."""
    if operation is ConfigOperation.PATCH:
        return {key: result[key] for key in ("added", "updated", "removed") if key in result}
    return {"removed": result.get("removed")}
