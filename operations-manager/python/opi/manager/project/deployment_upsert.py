"""The one use case behind every create-or-update of a deployment.

The v1 route, the v2 route and the task handler each asked for this in their own words: the
names were validated up to three times, the component references in one place and not the
other, and the outcome was assembled twice. What an upsert IS lives here: validate, write
through :class:`~opi.manager.project.deployment_mutations.DeploymentMutations`, roll the
result out to the deployment, and report it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from opi.manager.project.errors import ProjectChangeError, error_from_result
from opi.manager.service.rollout import RolloutRequest, roll_out
from opi.services.project_utils import normalize_container_image
from opi.services.tasks import progress_steps as steps
from opi.services.tasks.task_errors import CLIENT_ERROR_TYPES
from opi.utils.naming import sanitize_kubernetes_name
from opi.utils.project_names import validate_project_name

if TYPE_CHECKING:
    from opi.manager.project_manager import ProjectManager
    from opi.manager.service.progress import ProgressReporter

#: What an entrance is told when the project name cannot be one.
INVALID_PROJECT_NAME = (
    "Invalid project name format. Must start with lowercase letter, "
    "then lowercase letters a-z, numbers 0-9, dash -, maximum 20 characters"
)

#: What a refusal on the names is, as the API answer and the task result have always said.
VALIDATION_ERROR = "validation_error"


def invalid_deployment_name(deployment_name: str) -> str | None:
    """Why this deployment name cannot be used, or None when it can.

    Shared with the entrances, so the route refuses the same names the use case does.
    """
    suggestion = sanitize_kubernetes_name(deployment_name)
    if suggestion == deployment_name.lower():
        return None
    return f"Invalid deployment name. Use lowercase letters, numbers, and hyphens only. Suggested: {suggestion}"


@dataclass(frozen=True)
class DeploymentUpsert:
    """One create-or-update of a deployment, whichever entrance asked for it.

    ``components`` carries objects with ``reference`` and ``image``; the entrances build
    them from their own request model. ``rollout`` False saves without reconciling.
    """

    project_name: str
    deployment_name: str
    components: list[Any] = field(default_factory=list)
    clone_from: str | None = None
    force_clone: bool = False
    domain_format: str | None = None
    subdomain: str | None = None
    base_domain: str | None = None
    environment: str | None = None
    actor: str | None = None
    rollout: bool = True


@dataclass(frozen=True)
class DeploymentUpsertResult:
    """What the upsert produced, in the fields every entrance reports on."""

    created: bool
    urls: dict[str, dict[str, Any]]
    approvals: list[Any]
    warnings: list[Any] | None
    processing: dict[str, Any]

    @property
    def succeeded(self) -> bool:
        return self.processing.get("status") != "failed"

    def components_answer(self, change: DeploymentUpsert) -> list[dict[str, str]]:
        """The component list as the answer reports it: the image normalized once."""
        return [
            {"reference": component.reference, "image": normalize_container_image(component.image)[0]}
            for component in change.components
        ]


async def upsert_deployment(
    change: DeploymentUpsert, project_manager: ProjectManager, progress: ProgressReporter
) -> DeploymentUpsertResult:
    """Create or update the deployment, and roll the result out to it.

    The component references are checked by the mutation itself, on the state it reads: the
    entrances check them too, against the cache, so a mistake answers 400 instead of a task
    that fails, and the project can change between the two moments.

    Raises:
        ProjectChangeError: when the upsert cannot be made; ``error_type`` says why, and the
            entrance turns that into its own refusal.
    """
    names_task = progress.add_task(steps.NAMES_CHECK)
    refusal = _why_not(change)
    if refusal is not None:
        progress.fail_task(names_task, refusal, client_error=True)
        progress.fail_project(refusal)
        raise ProjectChangeError(refusal, VALIDATION_ERROR)
    progress.complete_task(names_task)

    write_task = progress.add_task(steps.DEPLOYMENT_SAVE, subject=change.deployment_name)
    result = await project_manager.upsert_deployment(
        deployment_name=change.deployment_name,
        components=change.components,
        clone_from=change.clone_from,
        force_clone=change.force_clone,
        domain_format=change.domain_format,
        subdomain=change.subdomain,
        base_domain=change.base_domain,
        environment=change.environment,
        actor=change.actor,
    )
    if not result["success"]:
        error = error_from_result(result, "Unknown upsert error")
        progress.fail_task(write_task, str(error), client_error=error.error_type in CLIENT_ERROR_TYPES)
        progress.fail_project(str(error))
        raise error
    progress.complete_task(write_task)

    processing = await roll_out(
        RolloutRequest(
            project_name=change.project_name,
            deployment_names=[change.deployment_name],
            changed=True,
            requested=change.rollout,
            force_clone=change.force_clone,
        ),
        project_manager,
        progress,
    )
    urls = _urls(change, project_manager, progress) if change.rollout else {}
    if processing.get("status") == "completed" and (failures := project_manager.get_component_failures()):
        processing = {**processing, "component_failures": failures}
    return DeploymentUpsertResult(
        created=bool(result.get("created", False)),
        urls=urls,
        approvals=result.get("approvals", []),
        warnings=result.get("warnings") or None,
        processing=processing,
    )


def _why_not(change: DeploymentUpsert) -> str | None:
    """Why these names cannot be written, or None when they can."""
    if not validate_project_name(change.project_name):
        return INVALID_PROJECT_NAME
    return invalid_deployment_name(change.deployment_name)


def _urls(
    change: DeploymentUpsert, project_manager: ProjectManager, progress: ProgressReporter
) -> dict[str, dict[str, Any]]:
    """The public addresses this round produced, also reported into the progress panel."""
    urls: dict[str, dict[str, Any]] = {}
    for name, result in project_manager.get_deployment_results(change.deployment_name).items():
        urls[name] = {"cluster": result.cluster, "urls": result.urls}
        for url_name, url_value in (result.urls or {}).items():
            progress.update_component_web_address(url_name, url_value)
    return urls
