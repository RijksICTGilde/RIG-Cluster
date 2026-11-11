"""Rolling a saved project-file change out to the deployments it touched."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from opi.services.tasks.task_rollout import note_rollout_skipped, skipped_processing

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.manager.service.progress import ProgressReporter
    from opi.runtime.tasks.persistent_task_progress import PersistentTaskProgressManager


@dataclass(frozen=True)
class RolloutRequest:
    """Which deployments a written change has to reach, and whether it may.

    ``deployment_names`` None means project-wide; a list narrows the round to exactly those
    deployments and has to match the task scope recorded at creation time.
    """

    project_name: str
    deployment_names: list[str] | None
    changed: bool
    requested: bool


async def roll_out(
    request: RolloutRequest, project_manager: ProjectManager, progress: ProgressReporter
) -> dict[str, Any]:
    """The ``processing`` block a write reports, after processing what it has to."""
    if not request.requested:
        note_rollout_skipped(progress)
        return skipped_processing()
    if not request.changed:
        return {"status": "skipped"}

    succeeded = await project_manager.process_project_from_git(
        f"projects/{request.project_name}.yaml",
        # The task manager satisfies ``ProgressReporter``; the processing still types the
        # concrete class, which a use case must not depend on.
        task_progress_manager=cast("PersistentTaskProgressManager", progress),
        deployment_names=request.deployment_names,
    )
    if succeeded:
        return {"status": "completed"}
    error = project_manager.get_processing_error()
    progress.fail_project(error or "Project processing failed")
    return {"status": "failed", **({"error": error} if error else {})}
