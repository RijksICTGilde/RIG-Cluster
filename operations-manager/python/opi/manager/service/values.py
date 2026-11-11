"""The one use case behind every write of the values a service owns on a component.

``user-env-vars`` and ``aliases`` are one block each, so every change is the same round
trip: decrypt, apply the operation, re-encrypt, commit, reconcile the deployments that
use the component. The v2 values routes, the env-vars window and the task handler all
ask for that round trip here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from opi.manager.service.configure import INVALID_PROJECT_NAME
from opi.manager.service.errors import InvalidProjectNameError, error_from_result
from opi.manager.service.rollout import RolloutRequest, roll_out
from opi.services.tasks import progress_steps as steps
from opi.utils.project_names import validate_project_name

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.manager.service.progress import ProgressReporter
    from opi.services.catalog.base import ConfigLayer
    from opi.services.component_values import ValuesOperation

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ComponentValuesChange:
    """One change to the values a service owns on one component.

    ``deployment_names`` is the rollout scope the entrance resolved with
    ``affected_deployments``; None means project-wide.
    """

    project_name: str
    service_name: str
    layer: ConfigLayer
    operation: ValuesOperation
    component_name: str
    deployment_name: str | None = None
    values: dict[str, str] | None = None
    keys: list[str] | None = None
    rollout: bool = True
    deployment_names: list[str] | None = None


@dataclass(frozen=True)
class ComponentValuesResult:
    """Whether the stored set really changed, and what the rollout did about it."""

    changed: bool
    processing: dict[str, Any]

    @property
    def succeeded(self) -> bool:
        return self.processing.get("status") != "failed"


async def set_component_values(
    change: ComponentValuesChange, project_manager: ProjectManager, progress: ProgressReporter
) -> ComponentValuesResult:
    """Apply ``change`` to the project file and roll it out to the deployments it touches.

    Raises a :class:`~opi.manager.service.errors.ServiceChangeError` when the change cannot
    be made; the entrance turns that into its own refusal. ``changed`` is False when the
    resulting values equal what is already stored, and then nothing is rolled out either.
    """
    name_task = progress.add_task(steps.PROJECT_NAME_CHECK)
    if not validate_project_name(change.project_name):
        progress.fail_task(name_task, INVALID_PROJECT_NAME)
        progress.fail_project(INVALID_PROJECT_NAME)
        raise InvalidProjectNameError(INVALID_PROJECT_NAME)
    progress.complete_task(name_task)

    write_task = progress.add_task(
        steps.VALUES_SAVE, subject=f"{change.operation.value} van {change.service_name} op {change.layer.value}"
    )
    result = await project_manager.set_component_values(
        change.service_name,
        change.layer.value,
        change.operation.value,
        component_name=change.component_name,
        deployment_name=change.deployment_name,
        values=change.values,
        keys=change.keys,
    )
    if not result["success"]:
        error = error_from_result(result, "Unknown error writing service values")
        progress.fail_task(write_task, str(error))
        progress.fail_project(str(error))
        raise error
    progress.complete_task(write_task)

    changed = bool(result.get("changed"))
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
    return ComponentValuesResult(changed=changed, processing=processing)
