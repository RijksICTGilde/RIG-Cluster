"""The one use case behind every removal of a deployment.

The API routes, the web route, the task handler, a project deletion and a deployment that
disappears from the project file all ask for the same thing: the ArgoCD application, the
namespace, the service resources, the manifests and the subdomains of one deployment go
away. They used to ask two different implementations, which differed in order, retention
and error handling. They all ask this one now, and only differ in what they hand in.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from opi.connectors import create_argo_connector
from opi.core.cluster_config import get_argo_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.manager.project.argocd_files import namespace_used_by_others, remove_appproject_file
from opi.services import ServiceType
from opi.services.persistence.subdomain_registry import SubdomainConnector
from opi.services.project import Project
from opi.services.project_store import get_project_store
from opi.services.tasks import progress_steps as steps
from opi.utils.naming import (
    generate_argocd_application_name,
    generate_argocd_appproject_prefix,
    generate_backup_prefix,
    generate_deployment_manifest_path,
    generate_gitops_argocd_application_path,
    get_output_filename_from_template,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Collection

    from opi.connectors.argo import ArgoConnector
    from opi.manager.database_manager import DatabaseManager
    from opi.manager.delete_project_manager import DeleteProjectManager
    from opi.services.marked_for_deletion_service import MarkedForDeletionService

logger = logging.getLogger(__name__)

#: The two spellings each of these services has in a project file.
_DATABASE_SERVICES = (ServiceType.POSTGRESQL_DATABASE.value, ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value)
_REDIS_SERVICES = (ServiceType.REDIS.value, ServiceType.NAMESPACE_REDIS.value)


class DeploymentRemovalError(Exception):
    """A deployment that cannot be removed as asked."""


class ProjectNotFoundError(DeploymentRemovalError):
    """The project this removal names does not exist on this cluster."""


class DeploymentNotFoundError(DeploymentRemovalError):
    """The deployment this removal names does not exist in the project on this cluster."""


class RemovalTrigger(StrEnum):
    """What asked for the removal, for the two things that still follow from it."""

    #: A delete call: the entry still stands in the project file and this removal writes it out.
    EXPLICIT = "explicit"
    #: The entry already left the project file, so a failed GitOps commit is reported and the
    #: rest of the cleanup still runs.
    PROJECT_FILE_EDIT = "project_file_edit"


@dataclass(frozen=True)
class DeploymentRemoval:
    """One removal of one deployment, whichever entrance asked for it."""

    project_name: str
    deployment_name: str
    trigger: RemovalTrigger
    #: The deployment entry itself. The project-file path hands in the entry from the
    #: PREVIOUS file, because the current one no longer has it.
    deployment: dict[str, Any] | None = None
    #: The project file as it stands now. Read from git when it is not handed in.
    project_data: dict[str, Any] | None = None
    #: The project file from before the edit, for the service detection of a deployment the
    #: current file no longer describes.
    previous_project_data: dict[str, Any] | None = None
    force: bool = False
    #: The deployments going away in the SAME round. A sibling from that set does not hold
    #: the namespace, because it is going away just as hard.
    leaving: Collection[str] = ()


@dataclass(frozen=True)
class DeploymentRemovalResult:
    """What the removal left behind.

    ``details`` is the dictionary every entrance has always answered with, so the answer of
    a route and of a task does not change shape.
    """

    details: dict[str, Any] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """Whether the re-measurement found the deployment gone on every layer."""
        return bool(self.details.get("success"))

    @property
    def already_absent(self) -> bool:
        """Whether there was nothing to remove."""
        return bool(self.details.get("already_absent"))

    @property
    def errors(self) -> list[str]:
        """Everything that went wrong, in the order it happened."""
        return list(self.details.get("errors") or [])

    @property
    def error_detail(self) -> str:
        """The errors as one sentence, for a caller that reports a failure."""
        return "; ".join(str(error) for error in self.errors) or "unknown error"


def find_removable_deployment(project_data: dict[str, Any], deployment_name: str) -> dict[str, Any] | None:
    """The deployment this cluster may remove, or ``None``.

    The cluster filter is the same one ``get_deployments`` applies: an instance only removes
    what it manages, so a name that belongs to another cluster is not found here either.
    """
    deployment = Project(project_data).find("deployments", name=deployment_name)
    if deployment is None:
        return None
    return deployment if deployment.get("cluster") == settings.CLUSTER_MANAGER else None


async def marking_service() -> MarkedForDeletionService | None:
    """The service that marks persistent data for the purge, or ``None`` when the DB is down.

    Every removal marks first: the grace period of ``DELETION_GRACE_PERIOD_DAYS`` is what
    makes a delete recoverable. Only an unreachable database falls back to wiping at once,
    because leaving the data behind unmarked would leak it instead.
    """
    from opi.core.database_pools import get_database_pool
    from opi.services.marked_for_deletion_service import MarkedForDeletionService

    try:
        get_database_pool("main")
    except KeyError, ValueError:
        logger.warning("Database pool not available - persistent resources will be deleted immediately")
        return None
    return MarkedForDeletionService()


async def remove_deployment(request: DeploymentRemoval, deletes: DeleteProjectManager) -> DeploymentRemovalResult:
    """Remove one deployment and everything that belongs to it.

    The order is fixed: the ArgoCD application goes first and is waited out, because its
    finalizer needs the manifests it is about to lose; the services follow, so persistent
    data is marked while the namespace still stands; the namespace, the manifests, the
    project file and the subdomains close it off. A re-measurement decides the outcome,
    because the error list is a log and not a state.

    Raises:
        ProjectNotFoundError: the project is not on this cluster (never under ``force``).
        DeploymentNotFoundError: the deployment is not in the project (never under ``force``).
        DeploymentRemovalError: a step broke off the removal (never under ``force``).
    """
    results = _new_results(request)
    try:
        run = await _resolve(request, deletes, results)
        await _detach_application(run)
        await _remove_service_resources(run)
        await _mark_backup_data(run)
        await _settle_namespace(run)
        await _settle_manifests(run)
        await _remove_from_project_file(run)
        await _remove_subdomains(run)
        await _measure_outcome(run)
    except (ProjectNotFoundError, DeploymentNotFoundError) as absent:
        if not request.force:
            raise
        # Idempotent, and visibly so: the caller asked to remove something that was not
        # there, and gets told that instead of "deleted".
        logger.info("Removal of %s/%s: %s - treating as deleted", request.project_name, request.deployment_name, absent)
        results["already_absent"] = True
        results["operations"].append(
            {
                "type": "deployment_deletion",
                "target": request.deployment_name,
                "status": "not_found",
                "reason": str(absent),
            }
        )
        return DeploymentRemovalResult(results)
    except Exception as e:
        results["success"] = False
        results["errors"].append(f"Critical error during deployment deletion: {e}")
        logger.exception("Critical error removing %s/%s", request.project_name, request.deployment_name)
        if not request.force:
            raise DeploymentRemovalError(str(e)) from e
        logger.warning("Force mode: returning results with critical error instead of raising")
        return DeploymentRemovalResult(results)

    if request.force and results["errors"]:
        logger.warning(
            "Force mode finished with %d error(s) for %s/%s",
            len(results["errors"]),
            request.project_name,
            request.deployment_name,
        )
    logger.info(
        "Removal of %s/%s completed - Success: %s, Force: %s, Errors: %d",
        request.project_name,
        request.deployment_name,
        results["success"],
        request.force,
        len(results["errors"]),
    )
    return DeploymentRemovalResult(results)


def _new_results(request: DeploymentRemoval) -> dict[str, Any]:
    """The answer dictionary every entrance has always read, before anything ran."""
    return {
        "project": request.project_name,
        "deployment": request.deployment_name,
        "trigger": request.trigger.value,
        "operations": [],
        "success": True,
        "errors": [],
        "service_results": {},
        "force_mode": request.force,
        # "It is gone" and "it was never there" are both success, and a script cannot act on
        # the difference unless the answer states it (RC-66).
        "already_absent": False,
    }


@dataclass
class _Run:
    """One removal in flight: what it works on, and what the steps so far established."""

    request: DeploymentRemoval
    deletes: DeleteProjectManager
    results: dict[str, Any]
    deployment: dict[str, Any]
    project_data: dict[str, Any]
    #: The project file the service checks read: the deployment has to be IN it, so the
    #: project-file path reads the version from before the edit.
    service_data: dict[str, Any]
    marked: MarkedForDeletionService | None
    cluster: str
    app_name: str
    base_namespace: str | None
    namespace_used_by_others: bool
    repository_name: str | None
    appproject_left_git: bool = False
    argocd_app_deleted: bool = False
    #: Only a namespace this run tried to delete may be re-measured; a marked one stands on
    #: purpose until the purge takes it.
    namespace_deletion_attempted: bool = False
    manifest_folder_removed: bool = True
    project_file_settled: bool = True

    @property
    def _namespace_name(self) -> str:
        """The namespace this deployment declares, or the project name as the file's default."""
        return self.base_namespace or self.request.project_name

    @property
    def namespace(self) -> str:
        """The prefixed namespace of this deployment."""
        return get_prefixed_namespace(self.cluster, self._namespace_name)

    @property
    def appproject_name(self) -> str:
        """The AppProject this deployment's namespace belongs to."""
        return generate_argocd_appproject_prefix(self.request.project_name, self._namespace_name)

    def begin(self, name: str) -> tuple[str | None, int]:
        """Open a progress step, and remember how many errors there were before it."""
        return self.deletes._begin_step(name, subject=self.request.deployment_name), len(self.results["errors"])

    def end(self, step: tuple[str | None, int]) -> None:
        """Close a progress step with whatever errors it added as its reason."""
        step_id, errors_before = step
        self.deletes._end_step(step_id, self.deletes._new_errors(self.results, errors_before))

    def record(self, **operation: object) -> None:
        """Add one operation to the answer."""
        self.results["operations"].append(operation)

    def fail(self, message: str) -> None:
        """Record an error. The outcome is decided by the re-measurement, not by this."""
        self.results["errors"].append(message)


async def _resolve(request: DeploymentRemoval, deletes: DeleteProjectManager, results: dict[str, Any]) -> _Run:
    """Find the deployment and everything the steps need, or refuse."""
    project_manager = deletes.project_manager
    deployment = request.deployment
    project_data = request.project_data

    if deployment is None:
        project = get_project_store().get(request.project_name)
        if not project:
            raise ProjectNotFoundError(f"Project '{request.project_name}' not found in project service")
        project_manager._project_file_relative_path = f"projects/{project.filename}"
        git_connector = await project_manager.get_git_connector_for_project_files()
        await git_connector.ensure_repo_cloned()
        project_data = await project_manager.get_contents()
        deployment = await project_manager.get_deployment_by_name(request.deployment_name)
        if not deployment:
            raise DeploymentNotFoundError(
                f"Deployment '{request.deployment_name}' not found in project '{request.project_name}'"
            )
    if project_data is None:
        project_data = await project_manager.get_contents()

    cluster = deployment.get("cluster", "")
    base_namespace = deployment.get("namespace")
    logger.info("Removing deployment %s/%s (cluster=%s)", request.project_name, request.deployment_name, cluster)
    if request.force:
        logger.info("Force mode enabled for %s/%s", request.project_name, request.deployment_name)

    return _Run(
        request=request,
        deletes=deletes,
        results=results,
        deployment=deployment,
        project_data=project_data,
        service_data=request.previous_project_data or project_data,
        marked=await marking_service(),
        cluster=cluster,
        app_name=generate_argocd_application_name(request.project_name, request.deployment_name),
        base_namespace=base_namespace,
        namespace_used_by_others=namespace_used_by_others(
            project_data.get("deployments", []), deployment, request.leaving
        ),
        repository_name=deployment.get("repository"),
        # The project-file path has nothing to write back: the user removed the entry, so
        # the file is the input of this removal and already settled.
        project_file_settled=request.trigger is RemovalTrigger.PROJECT_FILE_EDIT,
    )


async def _detach_application(run: _Run) -> None:
    """Remove the ArgoCD files from git and wait until ArgoCD has really dropped the app.

    One progress step: for the user this is one wait, and the commit only starts it.
    """
    step = run.begin(steps.ARGOCD_APP_DELETE)
    try:
        if await _remove_argocd_files(run):
            await _await_application_gone(run)
    except Exception as e:
        run.deletes._end_step(step[0], str(e))
        raise
    run.end(step)


@dataclass
class _Failure:
    """Whether the block inside :func:`_recorded` broke, for a caller that has to know."""

    error: str | None = None


@asynccontextmanager
async def _recorded(run: _Run, message: str, as_error: bool = True, **operation: object) -> AsyncIterator[_Failure]:
    """Record a broken step and let the removal go on.

    The only place in this module that catches broadly, and the reason is the same every
    time: a removal walks layers that do not depend on each other, so one that breaks must
    not leave the others standing. The outcome is decided by the re-measurement at the end,
    so an error caught here cannot turn a half removal green.

    ``as_error=False`` for a step whose failure says nothing about what was left behind, such
    as a refresh: it is recorded and logged, but does not count as a defect of the removal.
    """
    failure = _Failure()
    try:
        yield failure
    except Exception as e:
        failure.error = str(e)
        if as_error:
            run.fail(f"{message}: {e}")
        if operation:
            run.record(**operation, error=str(e))
        logger.exception(message)


def _remove_repository_files(project_dir: str, unique_repo_name: str) -> None:
    """Drop the repository secrets of a repository no other deployment still uses."""
    for template in ["argo-repository-https.yaml.jinja", "argo-repository.yaml.jinja"]:
        file_path = os.path.join(project_dir, get_output_filename_from_template(template, unique_repo_name))
        if os.path.exists(file_path):
            os.remove(file_path)
            logger.info("Deleted repository file: %s", os.path.basename(file_path))


async def _remove_argocd_files(run: _Run) -> bool:
    """Take the Application, its AppProject and its repository secrets out of the GitOps repo.

    They go in ONE mutation: removing them after the commit left their removal uncommitted
    in the working tree, while the kustomization rebuilt just before it still named them.

    Returns whether the commit landed, so a project-file edit that could not commit does not
    then wait for an application nothing asked to delete.
    """
    project_name = run.request.project_name
    deployment_name = run.request.deployment_name
    project_manager = run.deletes.project_manager

    gitops_workspace = await project_manager.get_argo_workspace()
    app_file_path = generate_gitops_argocd_application_path(run.cluster, project_name, deployment_name)
    working_dir = await gitops_workspace.get_working_dir()
    project_dir = os.path.join(working_dir, run.cluster, project_name)
    file_full_path = os.path.join(working_dir, app_file_path)

    repo_used_by_others = run.repository_name is not None and any(
        other.get("name") != deployment_name and other.get("repository") == run.repository_name
        for other in run.project_data.get("deployments", [])
    )

    async def _mutate() -> None:
        if os.path.exists(file_full_path):
            os.remove(file_full_path)
            run.record(
                type="argocd_application_file_deletion",
                target=app_file_path,
                cluster=run.cluster,
                deployment=deployment_name,
                status="success",
            )
            logger.info("Deleted ArgoCD application file: %s", app_file_path)
        else:
            run.record(
                type="argocd_application_file_deletion",
                target=app_file_path,
                cluster=run.cluster,
                deployment=deployment_name,
                status="not_found",
            )

        if run.base_namespace:
            run.appproject_left_git = remove_appproject_file(
                project_dir, project_name, run.base_namespace, run.namespace_used_by_others
            )

        if run.repository_name and not repo_used_by_others:
            _remove_repository_files(project_dir, f"{project_name}-{run.repository_name}")

        # After the removals: before them the kustomization still named what was gone after.
        if os.path.isdir(project_dir):
            rebuilt = project_manager._manifest_generator.create_kustomization_files(
                output_dir=project_dir,
                namespace=get_argo_namespace(run.cluster),
            )
            run.record(
                type="kustomization_rebuild",
                target=project_dir,
                cluster=run.cluster,
                status="success" if rebuilt else "failed",
            )
            if not rebuilt:
                logger.error("Failed to rebuild kustomization.yaml for project %s", project_name)

    commit_message = (
        f"Delete ArgoCD application for deployment '{deployment_name}' from project "
        f"'{project_name}' and rebuild kustomization"
    )
    try:
        await gitops_workspace.mutate_and_push(commit_message, _mutate)
    except Exception as e:
        if run.request.trigger is RemovalTrigger.EXPLICIT:
            # Let git exceptions bubble up for worker retry: the entry is still in the
            # project file, so a retry asks for exactly the same removal again.
            raise
        # The entry already left the project file, so there is no retry that asks for this
        # again: report the error and clean up what can still be cleaned up.
        run.fail(f"Error cleaning up ArgoCD resources: {e}")
        run.record(type="argocd_cleanup", status="error", error=str(e))
        logger.exception("Error cleaning up ArgoCD resources for %s/%s", project_name, deployment_name)
        return False

    run.record(type="gitops_commit", status="success", message=commit_message)
    return True


async def _await_application_gone(run: _Run) -> None:
    """Wait until ArgoCD has really removed the Application, and sweep it away when stuck."""
    argo_connector = create_argo_connector()

    async with _recorded(
        run,
        "Error refreshing user-applications",
        as_error=False,
        type="argocd_refresh",
        target="user-applications",
        status="error",
    ):
        refreshed = await argo_connector.refresh_application("user-applications")
        run.record(
            type="argocd_refresh",
            target="user-applications",
            status="success" if refreshed else "failed",
            **({} if refreshed else {"error": "Failed to refresh user-applications"}),
        )
        if not refreshed:
            logger.warning("Failed to refresh user-applications - continuing anyway")

    try:
        if await argo_connector.application_exists(run.app_name):
            await _wait_or_sweep(run, argo_connector)
        else:
            run.record(
                type="argocd_app_gitops_deletion",
                target=run.app_name,
                cluster=run.cluster,
                deployment=run.request.deployment_name,
                status="not_found",
            )
            logger.info("ArgoCD application %s was not found (already deleted or never existed)", run.app_name)
            run.argocd_app_deleted = True
    except PermissionError:
        # Permission denied means the AppProject was deleted before the Application, so the
        # Application is gone or will be garbage collected.
        run.record(
            type="argocd_app_gitops_deletion",
            target=run.app_name,
            cluster=run.cluster,
            deployment=run.request.deployment_name,
            status="success",
            note="Permission denied (AppProject deleted), treating as deleted",
        )
        logger.info("ArgoCD application %s - permission denied (AppProject deleted), treating as deleted", run.app_name)
        run.argocd_app_deleted = True
    except Exception as e:
        run.record(
            type="argocd_app_gitops_deletion",
            target=run.app_name,
            cluster=run.cluster,
            deployment=run.request.deployment_name,
            status="error",
            error=str(e),
        )
        logger.exception("Error monitoring ArgoCD application deletion")
        if run.request.force:
            run.argocd_app_deleted = True


async def _wait_or_sweep(run: _Run, argo_connector: ArgoConnector) -> None:
    """Wait out the GitOps deletion of the Application; under force, sweep a stuck one away."""
    from opi.manager.delete_project_manager import deletion_progress_window

    logger.info("Waiting for ArgoCD application %s to be deleted via GitOps", run.app_name)
    await run.deletes._terminate_application_operation(run.app_name, run.results)
    deletion_complete = await argo_connector.wait_for_application_deletion(
        run.app_name,
        max_retries=40,
        retry_delay=5,
        kubectl_connector=run.deletes.project_manager._kubectl_connector,
        progress_window_seconds=deletion_progress_window(),
    )

    if deletion_complete:
        run.record(
            type="argocd_app_gitops_deletion",
            target=run.app_name,
            cluster=run.cluster,
            deployment=run.request.deployment_name,
            status="success",
        )
        logger.info("ArgoCD application %s successfully deleted via GitOps", run.app_name)
        run.argocd_app_deleted = True
        return

    finalizer_removed = False
    if run.request.force:
        logger.warning(
            "ArgoCD application %s deletion timed out - force mode: deleting its resources, then removing finalizers",
            run.app_name,
        )
        outcome = await run.deletes._force_delete_or_await_cascade(
            run.app_name,
            run.results,
            argo_connector,
            await run.deletes._deployments_repo_dir(run.project_data, run.repository_name),
        )
        run.argocd_app_deleted = outcome.application_gone
        finalizer_removed = outcome.finalizer_removed
    else:
        logger.warning("ArgoCD application %s deletion timed out - continuing anyway", run.app_name)

    run.record(
        type="argocd_app_gitops_deletion",
        target=run.app_name,
        cluster=run.cluster,
        deployment=run.request.deployment_name,
        status="timeout",
        error="Application deletion via GitOps timed out",
        finalizer_removed=finalizer_removed,
    )


def _uses(run: _Run, service_types: Collection[str]) -> bool:
    """Whether the deployment used one of these services, read from the file that still has it."""
    return run.deletes.project_manager._project_file_handler.deployment_uses_service(
        run.service_data, run.request.deployment_name, list(service_types)
    )


async def _remove_service_resources(run: _Run) -> None:
    """Hand every service this deployment used its own removal.

    Before the namespace on purpose: the persistent ones are marked for the purge, and the
    backup key below can only be derived while the namespace still stands.
    """
    step = run.begin(steps.SERVICES_CLEANUP)
    logger.info("Removing service resources for %s/%s", run.request.project_name, run.request.deployment_name)

    keycloak_manager = run.deletes.project_manager._keycloak_manager
    await _run_service_removal(
        run, "keycloak", keycloak_manager.delete_resources_for_deployment(run.service_data, run.deployment)
    )

    managers = run.deletes.project_manager
    database = await _database_manager(run, step) if _uses(run, _DATABASE_SERVICES) else None
    for key, service_types, manager in (
        ("redis", _REDIS_SERVICES, managers._redis_manager),
        ("database", _DATABASE_SERVICES, database),
        ("minio", (ServiceType.MINIO_STORAGE.value,), managers._minio_manager),
        ("seaweedfs", (ServiceType.SEAWEEDFS_STORAGE.value,), managers._seaweedfs_manager),
        ("send_email", (ServiceType.SEND_EMAIL.value,), managers._mail_manager),
    ):
        if manager is None or not _uses(run, service_types):
            continue
        await _run_service_removal(
            run,
            key,
            manager.handle_service_removal(
                project_name=run.request.project_name,
                deployment_name=run.request.deployment_name,
                deployment_data=run.deployment,
                project_data=run.service_data,
                marked_for_deletion_service=run.marked,
            ),
        )

    run.end(step)


async def _database_manager(run: _Run, step: tuple[str | None, int]) -> DatabaseManager | None:
    """The database manager, or ``None`` when force mode has to do without it.

    Reaching it needs working credentials, which is exactly what a force delete may no
    longer have. Without force this breaks the removal off, because silently leaving a
    database behind is worse than failing.
    """
    try:
        return await run.deletes.project_manager._ensure_database_manager()
    except Exception as db_error:
        if not run.request.force:
            run.deletes._end_step(step[0], str(db_error))
            raise
        logger.warning("Force mode: could not reach the database manager (%s), skipping", db_error)
        run.results["service_results"]["database"] = {
            "operations": [],
            "errors": [str(db_error)],
            "skipped": True,
            "force_mode": True,
        }
        run.record(type="database_resource_deletion", status="skipped", reason=str(db_error), force_mode=True)
        return None


async def _run_service_removal(run: _Run, key: str, removal: Awaitable[dict[str, Any]]) -> None:
    """Await one service's removal and fold its answer into this one."""
    async with _recorded(run, f"Error removing {key} resources for {run.request.deployment_name}"):
        result = await removal
        run.results["service_results"][key] = result
        run.results["operations"].extend(result.get("operations", []))
        if result.get("errors"):
            run.results["errors"].extend(result["errors"])


def _has_marked_data(run: _Run) -> bool:
    """Whether this removal left data behind that the purge still has to take."""
    return run.marked is not None and (
        _uses(run, _DATABASE_SERVICES)
        or _uses(run, (ServiceType.MINIO_STORAGE.value,))
        or _uses(run, (ServiceType.SEAWEEDFS_STORAGE.value,))
    )


async def _mark_backup_data(run: _Run) -> None:
    """Mark the backup repository of this deployment, while its namespace still stands.

    The Kopia password is derived from the namespace, so it is read now and stored in the
    mark: the reconciliation job has to be able to open the repository after the namespace
    is gone.
    """
    if run.marked is None or not run.base_namespace:
        return

    from opi.manager.backup.base import BackupConfig, BaseBackupManager, get_backup_bucket_name

    namespace = run.namespace
    backup_bucket = get_backup_bucket_name(run.request.project_name, run.cluster)
    backup_prefix = generate_backup_prefix(run.cluster, namespace)
    backup_resource_name = f"{backup_bucket}/{backup_prefix}"

    backup_manager = BaseBackupManager(config=BackupConfig.from_settings())
    kopia_password = None
    # Not an error of the removal: the mark is made either way, the backup repository then
    # needs cleaning up by hand. That is what this path did before it was merged.
    async with _recorded(run, f"Could not derive the Kopia password for {namespace}", as_error=False) as derive:
        kopia_password = await backup_manager._derive_backup_key(namespace)
    if derive.error:
        logger.warning("Backups of %s may require manual cleanup: no Kopia password in the mark", namespace)

    await run.marked.mark_resource(
        resource_type="backup_data",
        resource_name=backup_resource_name,
        project_name=run.request.project_name,
        deployment_name=run.request.deployment_name,
        cluster=run.cluster,
        metadata={
            "s3_bucket": backup_bucket,
            "s3_prefix": backup_prefix,
            "kopia_password": kopia_password,
            "namespace": namespace,
        },
    )
    run.record(
        type="mark_for_deletion",
        resource_type="backup_data",
        resource_name=backup_resource_name,
        status="marked",
    )


async def _settle_namespace(run: _Run) -> None:
    """Delete the namespace, or mark it when data still has to be purged out of it."""
    if not run.base_namespace:
        return
    namespace = run.namespace

    if run.namespace_used_by_others:
        run.record(
            type="namespace_deletion",
            target=namespace,
            cluster=run.cluster,
            deployment=run.request.deployment_name,
            status="skipped",
            reason="Namespace still used by other deployments",
        )
        logger.info("Skipping namespace deletion - %s still used by other deployments", namespace)
        return

    marked = run.marked
    if marked is not None and _has_marked_data(run):
        # The data lives in this namespace, so the namespace goes when the purge takes it.
        await marked.mark_resource(
            resource_type="namespace",
            resource_name=namespace,
            project_name=run.request.project_name,
            deployment_name=run.request.deployment_name,
            cluster=run.cluster,
            metadata={"has_marked_pvcs": True},
        )
        run.record(type="mark_for_deletion", resource_type="namespace", resource_name=namespace, status="marked")
        return

    if not run.argocd_app_deleted and not run.request.force:
        run.record(
            type="namespace_deletion",
            target=namespace,
            cluster=run.cluster,
            deployment=run.request.deployment_name,
            status="skipped",
            reason="ArgoCD application not confirmed deleted - skipping to prevent orphaned app",
        )
        run.fail(
            f"Namespace '{namespace}' not deleted: ArgoCD application '{run.app_name}' was not confirmed deleted. "
            "Use force=true to override."
        )
        logger.warning("Skipping namespace deletion - ArgoCD app %s not confirmed deleted", run.app_name)
        return

    step = run.begin(steps.NAMESPACE_DELETE)
    if not run.argocd_app_deleted:
        logger.warning("Force mode: deleting namespace %s even though ArgoCD app status is uncertain", namespace)
    run.namespace_deletion_attempted = True
    deleted = False
    async with _recorded(
        run,
        f"Error deleting namespace {namespace}",
        type="namespace_deletion",
        target=namespace,
        cluster=run.cluster,
        deployment=run.request.deployment_name,
        status="error",
    ) as delete_failure:
        deleted = await run.deletes.project_manager._kubectl_connector.delete_namespace(namespace)
    if delete_failure.error:
        run.end(step)
        return

    if deleted:
        run.record(
            type="namespace_deletion",
            target=namespace,
            cluster=run.cluster,
            deployment=run.request.deployment_name,
            status="success",
        )
        logger.info("Successfully deleted namespace: %s", namespace)
    else:
        # delete_namespace uses --ignore-not-found, so a genuinely absent namespace returns
        # True. A False here is a real delete failure (e.g. an RBAC 403), NOT "already gone".
        run.record(
            type="namespace_deletion",
            target=namespace,
            cluster=run.cluster,
            deployment=run.request.deployment_name,
            status="error",
            error="kubectl delete namespace failed (see logs); namespace left behind",
        )
        run.fail(f"Failed to delete namespace '{namespace}' (see logs for the kubectl error).")
        logger.error("Failed to delete namespace %s; it was left behind", namespace)
    run.end(step)


async def _settle_manifests(run: _Run) -> None:
    """Remove the generated manifests, or mark them when the Application still holds them.

    Only once the Application is confirmed gone: its resources-finalizer needs the source
    path to know which resources to clean up, and losing it mid-finalize deadlocks it.
    """
    step = run.begin(steps.MANIFESTS_DELETE) if not run.argocd_app_deleted or run.repository_name else None

    if not run.argocd_app_deleted:
        await _mark_manifests_for_retry(run)
    elif run.repository_name and run.cluster:
        await _remove_manifest_folder(run)

    if step is not None:
        run.end(step)


async def _mark_manifests_for_retry(run: _Run) -> None:
    """Leave the manifests in git and hand them to the purge, which retries the removal."""
    from opi.services.marked_for_deletion_service import MarkedForDeletionService

    logger.warning(
        "Skipping manifest deletion for %s/%s - ArgoCD application not confirmed deleted. "
        "Marking for deferred cleanup.",
        run.request.project_name,
        run.request.deployment_name,
    )
    async with _recorded(run, "Could not mark the manifests for deferred deletion") as marking:
        await MarkedForDeletionService().mark_resource(
            resource_type="deployment_manifests",
            resource_name=f"{run.cluster}/{run.request.project_name}/{run.request.deployment_name}",
            project_name=run.request.project_name,
            deployment_name=run.request.deployment_name,
            cluster=run.cluster,
            metadata={"repository_name": run.repository_name, "argocd_app_name": run.app_name},
        )
        run.record(
            type="deployment_folder_deletion",
            status="deferred",
            reason="ArgoCD application not confirmed deleted - marked for retry",
        )
    if marking.error:
        logger.warning("Manifests will remain in git until manually cleaned up")
        run.record(
            type="deployment_folder_deletion",
            status="skipped",
            reason="ArgoCD app not deleted and could not mark for retry",
        )


async def _remove_manifest_folder(run: _Run) -> None:
    """Take the deployment's manifest folder out of its repository."""
    repo_config = Project(run.project_data).find("repositories", name=run.repository_name)
    if repo_config is None:
        return

    async with _recorded(
        run,
        f"Error deleting deployment folder from {run.repository_name}",
        type="deployment_folder_deletion",
        repository=run.repository_name,
        status="error",
    ) as removal:
        workspace = await run.deletes.project_manager.get_deployment_workspace(run.repository_name, repo_config)
        folder_path = generate_deployment_manifest_path(
            run.cluster, run.request.project_name, run.request.deployment_name, repo_config.get("path", "")
        )
        commit_message = f"Delete deployment '{run.request.deployment_name}' from project '{run.request.project_name}'"
        logger.info("Attempting to delete deployment folder: %s", folder_path)
        if await workspace.remove_and_push(folder_path, commit_message):
            run.record(
                type="deployment_folder_deletion",
                target=folder_path,
                repository=run.repository_name,
                cluster=run.cluster,
                status="success",
            )
            run.record(
                type="manifest_repo_commit",
                repository=run.repository_name,
                status="success",
                message=commit_message,
            )
            logger.info("Successfully deleted deployment folder: %s", folder_path)
        else:
            run.record(
                type="deployment_folder_deletion",
                target=folder_path,
                repository=run.repository_name,
                cluster=run.cluster,
                status="not_found",
            )
            logger.info("Deployment folder not found: %s", folder_path)
    if removal.error:
        run.manifest_folder_removed = False


async def _remove_from_project_file(run: _Run) -> None:
    """Take the entry out of the project file, once the removal is confirmed everywhere.

    The project file is the desired state every later run reads, so an entry removed while
    the resources still stand leaves nobody on either side asking for the deletion. Keeping
    it keeps the removal repeatable.

    The project-file path has no counterpart of this step: there the user removed the entry,
    so the file is the INPUT and there is nothing to write back.
    """
    if run.request.trigger is RemovalTrigger.PROJECT_FILE_EDIT:
        return

    deployment_name = run.request.deployment_name
    if not run.manifest_folder_removed or not run.argocd_app_deleted:
        reason = (
            "deployment manifests still in git"
            if not run.manifest_folder_removed
            else "ArgoCD application not confirmed deleted"
        )
        run.record(
            type="project_file_update",
            target=f"deployment '{deployment_name}'",
            action="removed_from_project_file",
            status="skipped",
            reason=f"{reason} - deployment kept in the project file so a retry can finish it",
        )
        logger.warning(
            "Deployment '%s' blijft in het projectbestand van '%s': %s",
            deployment_name,
            run.request.project_name,
            reason,
        )
        if not run.argocd_app_deleted:
            # The git errors above report themselves; an unconfirmed cluster delete does not,
            # and without an error the task would end green.
            run.fail(
                f"Deployment '{deployment_name}' was kept in the project file: the ArgoCD "
                f"application was not confirmed deleted"
            )
        return

    step = run.begin(steps.DEPLOYMENT_REMOVE_FROM_PROJECT_FILE)
    logger.info("Removing deployment '%s' from the project file of '%s'", deployment_name, run.request.project_name)

    def _without_this_deployment(project_data: dict[str, Any]) -> dict[str, Any] | None:
        deployments = project_data.get("deployments", []) or []
        remaining = [dep for dep in deployments if dep.get("name") != deployment_name]
        if len(remaining) == len(deployments):
            return None  # already absent -> idempotent no-op
        project_data["deployments"] = remaining
        return project_data

    async with _recorded(
        run,
        "Error removing deployment from project file",
        type="project_file_update",
        target=f"deployment '{deployment_name}'",
        action="removed_from_project_file",
        status="error",
    ):
        committed = await run.deletes.project_manager.mutate_and_commit_project(
            _without_this_deployment,
            f"Delete deployment '{deployment_name}' from project {run.request.project_name}",
        )
        run.record(
            type="project_file_update",
            target=f"deployment '{deployment_name}'",
            action="removed_from_project_file" if committed else "already_absent",
            status="success",
        )
        logger.info("Deployment '%s' removed from project file (committed=%s)", deployment_name, committed)
        run.project_file_settled = True
    run.end(step)


async def _remove_subdomains(run: _Run) -> None:
    """Drop the subdomain registrations of this deployment."""
    step = run.begin(steps.SUBDOMAIN_CLEANUP)
    async with _recorded(
        run, "Error cleaning up subdomain registrations for deployment", type="subdomain_cleanup", status="error"
    ):
        deleted = await SubdomainConnector().delete_by_deployment(run.request.project_name, run.request.deployment_name)
        if deleted:
            run.record(
                type="subdomain_cleanup",
                target=f"deployment '{run.request.project_name}/{run.request.deployment_name}'",
                status="success",
                count=deleted,
                message=f"Deleted {deleted} subdomain registration(s)",
            )
            logger.info("Deleted %d subdomain registration(s) for %s", deleted, run.request.deployment_name)
    run.end(step)


async def _measure_outcome(run: _Run) -> None:
    """Measure again whether the deployment is really gone, and let that decide the outcome.

    The error list is a log and not a state: an error that was true during the removal and no
    longer is at the end describes nothing left behind, while an empty error list does not
    make a namespace that stays standing go away. When the measurement could not run, the
    error list is the answer again: unknown is proof in neither direction.
    """
    step = run.begin(steps.DELETE_VERIFY)
    kubectl = run.deletes.project_manager._kubectl_connector

    if run.appproject_left_git:
        await kubectl.wait_for_resource_absence("appproject", run.appproject_name, get_argo_namespace(run.cluster))

    remaining = await run.deletes._measure_remaining(
        run.cluster,
        applications=[run.app_name],
        appprojects=[run.appproject_name] if run.appproject_left_git else [],
        namespaces=[run.namespace] if run.namespace_deletion_attempted else [],
    )

    if remaining.decides:
        settled = not remaining.present and run.project_file_settled
        run.results["success"] = settled
        for leftover in remaining.present:
            run.fail(
                f"Deployment '{run.request.deployment_name}' of project '{run.request.project_name}' is not "
                f"fully deleted: {leftover} still stands"
            )
        run.record(
            type="deletion_remeasurement",
            target=f"deployment '{run.request.deployment_name}'",
            status="clean" if settled else "incomplete",
            remaining=remaining.present,
            project_file_settled=run.project_file_settled,
            # The run's errors stay as an explanation next to a clean end state; they decide
            # nothing any more.
            explained_errors=run.results["errors"] if settled else [],
        )
    else:
        run.results["success"] = len(run.results["errors"]) == 0
        run.record(
            type="deletion_remeasurement",
            target=f"deployment '{run.request.deployment_name}'",
            status="unreadable",
            unreadable=remaining.unreadable,
        )
    run.end(step)
