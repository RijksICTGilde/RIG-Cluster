"""Project deletion manager for handling project and deployment deletion operations."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from opi.connectors import create_argo_connector
from opi.core.actor import get_actor
from opi.core.cluster_config import get_argo_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.manager.project.argocd_files import application_files
from opi.manager.project.deployment_removal import (
    DeploymentNotFoundError,
    DeploymentRemoval,
    DeploymentRemovalError,
    ProjectNotFoundError,
    RemovalTrigger,
    remove_deployment,
)
from opi.services import ServiceAdapter
from opi.services.catalog.base import RemovalContext
from opi.services.persistence.subdomain_registry import SubdomainConnector
from opi.services.postgres_scope import project_uses_dedicated_postgres
from opi.services.project import Project
from opi.services.project_store import get_project_store
from opi.services.registry import get_service
from opi.services.services import service_entry_name
from opi.services.services_enums import ManagerKey
from opi.services.tasks import progress_steps as steps
from opi.utils.argocd_tracking import LABEL_VALUE_MAX, may_be_cut_from
from opi.utils.naming import generate_project_admin_username, generate_project_realm_name
from opi.utils.rendered_kinds import rendered_kinds, rendered_types

if TYPE_CHECKING:
    from collections.abc import Collection

    from opi.connectors.argo import ArgoConnector
    from opi.services.marked_for_deletion_service import MarkedForDeletionService
    from opi.utils.argocd_tracking import TrackedResource
from opi.services.project_utils import project_level_deployment
from opi.utils.naming import (
    generate_argocd_application_name,
    generate_argocd_appproject_prefix,
    generate_argocd_project_application_name,
    generate_infrastructure_application_name,
    generate_infrastructure_argocd_folder_path,
    generate_project_level_manifest_path,
)

logger = logging.getLogger(__name__)

#: How long the waits of ONE task may run together WHILE the cascade visibly makes progress.
#: Once nothing moves any more, the wait stops on the standstill and not on this bound.
#:
#: A budget per task and not per wait, because two of the three callers sit in a loop over
#: the deployments: per wait, a project with three deployments would pass
#: TASK_WORKER_MAX_DURATION (1800s) on waiting alone, and that bound cuts the handler off
#: HARD, without the force route and without a result. 600s plus the ordinary windows of the
#: other waits leaves room for the force route and the git work after it.
#:
#: Charged on the CLOCK and not on the windows that were handed out, because a poll costs
#: more than its retry_delay: an inventory of the target namespace took 1.8s on a namespace
#: with 271 resources. A wait can therefore exceed its own window in wall-clock time, and
#: that comes off the next wait.
DELETION_PROGRESS_BUDGET_SECONDS = 600

#: The budget runs per asyncio task, which is exactly one task of the task worker, since it
#: runs every handler as its own task. A parameter would have to travel through three callers
#: and two loops in project_manager that do nothing with it themselves.
_DELETION_PROGRESS_DEADLINE: ContextVar[float | None] = ContextVar("deletion_progress_deadline", default=None)


def deletion_progress_window() -> int | None:
    """What this wait may still use of its task's progress budget.

    ``None`` means the budget is spent. The wait then keeps its ordinary window and so ends
    red in good order, with the force route behind it, instead of being cut off by the hard
    task bound in the middle of the delete.
    """
    now = time.monotonic()
    deadline = _DELETION_PROGRESS_DEADLINE.get()
    if deadline is None:
        _DELETION_PROGRESS_DEADLINE.set(now + DELETION_PROGRESS_BUDGET_SECONDS)
        return DELETION_PROGRESS_BUDGET_SECONDS
    remaining = int(deadline - now)
    return remaining if remaining > 0 else None


# How long to wait for the cascade an Application kept its finalizer for. All 15 forced
# deletes measured on 9 October 2026 were gone from the cluster within a few minutes.
CASCADE_WAIT_RETRIES = 36
CASCADE_WAIT_DELAY = 5


def _split_on_the_mark(
    inventory: list[TrackedResource], app_name: str, known_names: set[str] | None
) -> tuple[list[TrackedResource], list[TrackedResource]]:
    """Split an inventory into what ``app_name`` unambiguously owns, and what stays undecided.

    Equality is not enough on a mark that sits on the label cap (may_be_cut_from), and without
    the names nothing on the cap can be placed at all.

    Args:
        known_names: Every Application name on the cluster, or ``None`` when that read failed.
    """
    tracked: list[TrackedResource] = []
    undecidable: list[TrackedResource] = []
    for resource in inventory:
        if resource.app_name == app_name:
            ambiguous = resource.truncatable if known_names is None else may_be_cut_from(resource, known_names)
            (undecidable if ambiguous else tracked).append(resource)
        elif may_be_cut_from(resource, {app_name}):
            undecidable.append(resource)
    return tracked, undecidable


@dataclass(frozen=True)
class RemainingState:
    """What a re-measurement of the layers at the end of a deletion still found."""

    #: The layers that are still there, as a readable description each.
    present: list[str]
    #: The layers whose measurement itself failed, so they are unknown.
    unreadable: list[str]

    @property
    def decides(self) -> bool:
        """Whether this measurement decides the answer: something found, or nothing found with
        every layer measured.
        """
        return bool(self.present) or not self.unreadable


@dataclass(frozen=True)
class ForcedDeletion:
    """Result of forcing a stuck Application."""

    finalizer_removed: bool
    swept_partially: bool  # True if the finalizer remains because the sweep was partial


@dataclass(frozen=True)
class StuckApplicationOutcome:
    """How a stuck Application ended: gone or not, and by whose hand."""

    application_gone: bool
    finalizer_removed: bool


class DeleteProjectManager:
    def __init__(self, project_manager: ProjectManager) -> None:
        self.project_manager = project_manager

    # Progress steps, as in project_manager._begin_step.

    def _begin_step(self, name: str, subject: str | None = None) -> str | None:
        progress_manager = self.project_manager.get_progress_manager()
        if progress_manager is None:
            return None
        return progress_manager.add_task(name, subject=subject)

    def _end_step(self, step_id: str | None, error: str | None = None) -> None:
        progress_manager = self.project_manager.get_progress_manager()
        if progress_manager is None or step_id is None:
            return
        if error:
            progress_manager.fail_task(step_id, error)
        else:
            progress_manager.complete_task(step_id)

    @staticmethod
    def _new_errors(deletion_results: dict[str, Any], errors_before: int) -> str | None:
        """The errors added since ``errors_before``, as the closing reason of a step."""
        new = [str(error) for error in deletion_results["errors"][errors_before:]]
        return "; ".join(new) if new else None

    async def _terminate_application_operation(self, app_name: str, deletion_results: dict[str, Any]) -> None:
        """Clear a running sync operation on an Application before it is deleted.

        Why this has to come first: a sync operation that is waiting on health blocks the
        deletion for as long as it runs (features/argocd-vastgelopen-verwijdering.md).
        """
        kubectl = self.project_manager._kubectl_connector
        terminated = await kubectl.terminate_argocd_application_operation(app_name)
        deletion_results["operations"].append(
            {
                "type": "argocd_app_operation_termination",
                "target": app_name,
                "status": "success" if terminated else "failed",
            }
        )
        if not terminated:
            logger.warning(f"Could not clear a running operation on ArgoCD application {app_name}")

    async def _inventory_types(
        self, app_name: str, deployments_repo_dir: str | None
    ) -> tuple[list[str] | None, list[str] | None]:
        """The types for an inventory, as ``(resource_types, own_types)``.

        The Application names its own types in ``status.resources``, which is the cluster
        itself talking, so those may narrow the question, and that is what case 4 (Capsule)
        needs. The fallback comes from the tenant's manifest repo and may NOT narrow it: a repo
        that no longer names a child would otherwise let the sweep report clean while the
        resource stays standing. It only says which ``Forbidden`` is not a hole in the
        measurement.
        """
        kubectl = self.project_manager._kubectl_connector
        types = await kubectl.argocd_application_resource_types(app_name)
        if types:
            return types, None
        if deployments_repo_dir is None:
            return None, None
        served = await kubectl.list_namespaced_kinds()
        if served is None:
            return None, None
        kinds, unreadable = rendered_kinds(Path(deployments_repo_dir), settings.CLUSTER_MANAGER)
        if unreadable:
            logger.info(
                f"{len(unreadable)} manifest(s) template their kind, so they are not judged for completeness: "
                f"{', '.join(unreadable)}"
            )
        return None, rendered_types(kinds, served) or None

    async def _deployments_repo_dir(self, project_data: dict[str, Any], repository_name: str | None) -> str | None:
        """The directory in the deployments repo holding the ``{cluster}/`` tree, or ``None``."""
        if not repository_name:
            return None
        repo_config = next(
            (r for r in project_data.get("repositories", []) or [] if r.get("name") == repository_name), None
        )
        if not repo_config:
            return None
        # To READ only, and across every project, so not through this project's working copy:
        # a throwaway directory with the whole ``{cluster}/`` tree in it.
        return await self.project_manager.materialize_cluster_tree(repo_config)

    async def _force_delete_stuck_application(
        self, app_name: str, deletion_results: dict[str, Any], deployments_repo_dir: str | None = None
    ) -> ForcedDeletion:
        """Delete an Application that would not go away: its resources first, then its finalizer.

        Removing the finalizer is what makes the Application disappear, but it also cancels
        the cascade that would have deleted the resources underneath it. So they go first,
        selected by the mark ArgoCD put on them (opi.utils.argocd_tracking). Forcing without
        that step is what left 350 resources behind in rig-prd-mpfm-w3h (RC-226).

        The mark has to name this application UNAMBIGUOUSLY, because here a match is a delete
        (_split_on_the_mark). What stays ambiguous is reported instead of deleted.

        A sweep that ran but did not get everything (``partial``) keeps the finalizer in
        place: the cascade it carries is then the only route by which the rest is still
        deleted under its own Application, and cancelling that while resources stand is the
        exact shape that left the 350 behind. A sweep that could not run at all still forces,
        because nothing was deleted there either way and an Application that stays blocks the
        parent for everyone.

        Returns:
            Whether the finalizer was removed, and whether the sweep was partial.
        """
        kubectl = self.project_manager._kubectl_connector
        swept_partially = False

        destination = await kubectl.get_argocd_application_destination_namespace(app_name)
        if destination is None:
            deletion_results["operations"].append(
                {
                    "type": "argocd_app_tracked_resource_deletion",
                    "target": app_name,
                    "status": "unknown_namespace",
                }
            )
            deletion_results["errors"].append(
                f"Could not determine the destination namespace of ArgoCD application '{app_name}'; "
                "its resources may be left behind"
            )
        else:
            resource_types, own_types = await self._inventory_types(app_name, deployments_repo_dir)
            inventory = (
                await kubectl.list_tracked_resources(destination, resource_types, own_types)
                if resource_types or own_types
                else None
            )
            if inventory is None:
                # Not the same as an empty namespace: this read failed, so reporting it
                # as swept is the silent force this function exists to prevent.
                deletion_results["operations"].append(
                    {
                        "type": "argocd_app_tracked_resource_deletion",
                        "target": app_name,
                        "namespace": destination,
                        "status": "inventory_failed",
                    }
                )
                deletion_results["errors"].append(
                    f"Could not inventory namespace '{destination}' for resources of ArgoCD application "
                    f"'{app_name}'; its resources may be left behind"
                )
                if not (resource_types or own_types):
                    logger.error(
                        f"Neither ArgoCD nor the deployments repo said which resource types "
                        f"'{app_name}' owns, so there is nothing to inventory it over"
                    )
            else:
                resources, complete = inventory
                known_names = await self._known_application_names()
                tracked, undecidable = _split_on_the_mark(resources, app_name, known_names)
                failed = await kubectl.delete_tracked_resources(tracked)
                swept_partially = bool(failed) or bool(undecidable) or not complete
                deletion_results["operations"].append(
                    {
                        "type": "argocd_app_tracked_resource_deletion",
                        "target": app_name,
                        "namespace": destination,
                        "status": "partial" if swept_partially else "success",
                        "deleted": len(tracked) - len(failed),
                        "failed": len(failed),
                        "undecidable": len(undecidable),
                    }
                )
                if not complete:
                    # A half answer that passes for a whole one is the same silent force as
                    # a failed one: what is deleted here is only what kubectl got round to
                    # printing, and the types that stayed silent are where the rest stands.
                    deletion_results["errors"].append(
                        f"Namespace '{destination}' answered for only part of its resource types, so resources "
                        f"of ArgoCD application '{app_name}' outside that part may be left behind"
                    )
                if failed:
                    deletion_results["errors"].append(
                        f"{len(failed)} resource(s) of ArgoCD application '{app_name}' could not be deleted "
                        f"in namespace '{destination}'"
                    )
                if undecidable and known_names is None:
                    deletion_results["errors"].append(
                        "Could not read the ArgoCD Application names, so no label value on the "
                        f"{LABEL_VALUE_MAX}-character cap could be told apart from a neighbour's name"
                    )
                if undecidable:
                    deletion_results["errors"].append(
                        f"{len(undecidable)} resource(s) in namespace '{destination}' carry a label value on the "
                        f"{LABEL_VALUE_MAX}-character cap that is as much a whole Application name as a longer one "
                        f"cut to it, so whether they are '{app_name}'s or a living neighbour's is not decidable "
                        "from the mark; left standing. Which of the two it is, the resource itself shows."
                    )

        if swept_partially:
            deletion_results["operations"].append(
                {
                    "type": "argocd_app_finalizer_removal",
                    "target": app_name,
                    "status": "skipped_partial_sweep",
                }
            )
            deletion_results["errors"].append(
                f"ArgoCD application '{app_name}' was left standing with its finalizer: its resources are not "
                "verifiably gone, and removing the finalizer would cancel the cascade that can still delete them"
            )
            return ForcedDeletion(finalizer_removed=False, swept_partially=True)

        finalizer_removed = await kubectl.remove_argocd_application_finalizers(app_name)
        deletion_results["operations"].append(
            {
                "type": "argocd_app_finalizer_removal",
                "target": app_name,
                "status": "success" if finalizer_removed else "failed",
            }
        )
        return ForcedDeletion(finalizer_removed=finalizer_removed, swept_partially=False)

    async def _force_delete_or_await_cascade(
        self,
        app_name: str,
        deletion_results: dict[str, Any],
        argo_connector: ArgoConnector,
        deployments_repo_dir: str | None = None,
    ) -> StuckApplicationOutcome:
        """Force a stuck Application away, or wait for its cascade to complete."""
        kubectl = self.project_manager._kubectl_connector
        errors_before = len(deletion_results["errors"])
        forced = await self._force_delete_stuck_application(app_name, deletion_results, deployments_repo_dir)

        if forced.finalizer_removed:
            gone = await argo_connector.wait_for_application_deletion(
                app_name, max_retries=10, kubectl_connector=kubectl
            )
            return StuckApplicationOutcome(application_gone=gone, finalizer_removed=True)

        if not forced.swept_partially:
            return StuckApplicationOutcome(application_gone=False, finalizer_removed=False)

        logger.info(
            f"ArgoCD application {app_name} kept its finalizer after a partial sweep - "
            "waiting for the cascade that finalizer carries"
        )
        gone = await argo_connector.wait_for_application_deletion(
            app_name,
            max_retries=CASCADE_WAIT_RETRIES,
            retry_delay=CASCADE_WAIT_DELAY,
            kubectl_connector=kubectl,
        )
        cascade_wait: dict[str, Any] = {
            "type": "argocd_app_cascade_wait",
            "target": app_name,
            "status": "success" if gone else "timeout",
        }
        if gone:
            # The cascade deletes everything under the Application, the part the sweep could
            # not list included, so its reasons for keeping the finalizer no longer describe
            # anything that was left behind. They stay readable here, as history.
            cascade_wait["withdrawn_errors"] = deletion_results["errors"][errors_before:]
            del deletion_results["errors"][errors_before:]
        deletion_results["operations"].append(cascade_wait)
        return StuckApplicationOutcome(application_gone=gone, finalizer_removed=False)

    async def _measure_remaining(
        self,
        cluster: str,
        applications: Collection[str] = (),
        appprojects: Collection[str] = (),
        namespaces: Collection[str] = (),
    ) -> RemainingState:
        """Measure again whether the Application, the AppProject and the namespace are there.

        The error list is a log and not a state: the API is the truth (cases 4 and 5).
        """
        kubectl = self.project_manager._kubectl_connector
        present: list[str] = []
        unreadable: list[str] = []

        for app_name in applications:
            exists = await kubectl.argocd_application_exists(app_name)
            if exists is True:
                present.append(f"ArgoCD application '{app_name}'")
            elif exists is not False:
                unreadable.append(f"ArgoCD application '{app_name}'")

        argo_namespace = get_argo_namespace(cluster)
        for appproject_name in appprojects:
            exists = await kubectl.resource_exists("appproject", appproject_name, argo_namespace)
            if exists is True:
                present.append(f"ArgoCD AppProject '{appproject_name}'")
            elif exists is not False:
                unreadable.append(f"ArgoCD AppProject '{appproject_name}'")

        for namespace in namespaces:
            absent = await kubectl.namespace_absent(namespace)
            if absent is False:
                present.append(f"namespace '{namespace}'")
            elif absent is not True:
                unreadable.append(f"namespace '{namespace}'")

        if present:
            logger.warning(f"After the deletion these still stand: {', '.join(present)}")
        if unreadable:
            logger.warning(f"After the deletion these could not be measured: {', '.join(unreadable)}")
        return RemainingState(present=present, unreadable=unreadable)

    async def _known_application_names(self) -> set[str] | None:
        """Every ArgoCD Application name on this cluster, or ``None`` when the read failed.

        ``list_argocd_applications`` answers a failure with an empty list, and the
        Application being forced is itself still standing, so an empty answer is a failed
        read and never "there are no neighbours". Reading it as the latter is what decides
        destructively on a mark at the label cap.
        """
        applications = await self.project_manager._kubectl_connector.list_argocd_applications()
        if not applications:
            return None
        return {application.get("metadata", {}).get("name", "") for application in applications}

    async def _cleanup_orphaned_argocd_resources(self, project_name: str, deletion_results: dict[str, Any]) -> None:
        """Clean up ArgoCD Applications and AppProjects that match the project name pattern but
        are in no deployment of the project file.

        That covers a deployment removed from the YAML while its ArgoCD resources stayed, and
        an earlier deletion that failed halfway.
        """
        logger.info(f"Checking for orphaned ArgoCD resources for project '{project_name}'")

        try:
            argo_connector = create_argo_connector()
            kubectl = self.project_manager._kubectl_connector
            argo_namespace = get_argo_namespace(settings.CLUSTER_MANAGER)

            all_applications = await argo_connector.list_applications()
            orphaned_apps = [
                app
                for app in all_applications
                if app.get("metadata", {}).get("name", "").startswith(f"{project_name}-")
            ]

            if orphaned_apps:
                logger.warning(
                    f"Found {len(orphaned_apps)} orphaned ArgoCD application(s) for project '{project_name}'"
                )

                for app in orphaned_apps:
                    app_name = app.get("metadata", {}).get("name")
                    logger.info(f"Deleting orphaned ArgoCD application: {app_name}")

                    try:
                        await self._terminate_application_operation(app_name, deletion_results)
                        delete_success = await kubectl.delete_argocd_application(app_name, argo_namespace)
                        if delete_success:
                            deletion_results["operations"].append(
                                {
                                    "type": "orphaned_argocd_application_cleanup",
                                    "target": app_name,
                                    "status": "success",
                                    "message": f"Deleted orphaned ArgoCD application '{app_name}'",
                                }
                            )
                            logger.info(f"Successfully deleted orphaned application: {app_name}")
                        else:
                            deletion_results["operations"].append(
                                {
                                    "type": "orphaned_argocd_application_cleanup",
                                    "target": app_name,
                                    "status": "failed",
                                    "message": f"Failed to delete orphaned ArgoCD application '{app_name}'",
                                }
                            )
                            logger.error(f"Failed to delete orphaned application: {app_name}")
                    except Exception as e:
                        logger.exception(f"Error deleting orphaned application {app_name}")
                        deletion_results["errors"].append(f"Failed to delete orphaned application {app_name}: {e}")
            else:
                logger.info(f"No orphaned ArgoCD applications found for project '{project_name}'")

            # An AppProject is named {project_name}-{deployment_name} or
            # {project_name}-infrastructure, which `project_name in line` below matches.
            stdout, stderr, code = await kubectl._run_kubectl_command(
                ["get", "appproject", "-n", argo_namespace, "-o", "name"]
            )

            if code == 0 and stdout:
                appproject_names = [
                    line.replace("appproject.argoproj.io/", "")
                    for line in stdout.strip().split("\n")
                    if line.startswith("appproject.argoproj.io/") and project_name in line
                ]

                for appproject_name in appproject_names:
                    logger.info(f"Deleting orphaned ArgoCD AppProject: {appproject_name}")
                    try:
                        _, stderr, del_code = await kubectl._run_kubectl_command(
                            ["delete", "appproject", appproject_name, "-n", argo_namespace]
                        )
                        if del_code == 0:
                            deletion_results["operations"].append(
                                {
                                    "type": "orphaned_argocd_appproject_cleanup",
                                    "target": appproject_name,
                                    "status": "success",
                                    "message": f"Deleted orphaned ArgoCD AppProject '{appproject_name}'",
                                }
                            )
                            logger.info(f"Successfully deleted orphaned AppProject: {appproject_name}")
                        else:
                            logger.error(f"Failed to delete AppProject {appproject_name}: {stderr}")
                    except Exception:
                        logger.exception(f"Error deleting orphaned AppProject {appproject_name}")

        except Exception as e:
            logger.exception("Error during orphaned ArgoCD resource cleanup")
            deletion_results["errors"].append(f"Orphaned ArgoCD cleanup error: {e}")

    async def _release_project_applications(
        self, project_name: str, cluster: str, deletion_results: dict[str, Any]
    ) -> bool:
        """Take the Applications out of git and wait until they are gone; the AppProject can
        only follow once the directory is empty (case 1).
        """
        gitops_workspace = await self.project_manager.get_argo_workspace()
        working_dir = await gitops_workspace.get_working_dir()
        project_dir = os.path.join(working_dir, cluster, project_name)
        removed: list[str] = []
        unreadable: list[str] = []

        async def _delete_applications() -> bool:
            if not os.path.isdir(project_dir):
                return False
            applications, unreadable_files = application_files(project_dir)
            unreadable.extend(unreadable_files)
            for path, name, _project in applications:
                os.remove(path)
                removed.append(name)
            if not removed:
                return False
            self.project_manager._manifest_generator.create_kustomization_files(
                output_dir=project_dir,
                namespace=get_argo_namespace(cluster),
            )
            return True

        commit_message = f"Remove ArgoCD applications of project '{project_name}' before its AppProject"
        await gitops_workspace.mutate_and_push(commit_message, _delete_applications)
        if not removed and not unreadable:
            return True

        deletion_results["operations"].append(
            {
                "type": "project_application_file_deletion",
                "target": os.path.join(cluster, project_name),
                "status": "success",
                "applications": removed,
                "unreadable": sorted(set(unreadable)),
            }
        )
        argo_connector = create_argo_connector()
        await argo_connector.refresh_application("user-applications")

        # A file that could not be read could not be waited on either, and the directory goes
        # entirely after this, Application file and AppProject together, so it counts as an
        # Application that stays standing (case 1).
        standing: list[str] = sorted(set(unreadable))
        for name in removed:
            gone = await argo_connector.wait_for_application_deletion(
                name,
                max_retries=40,
                retry_delay=5,
                kubectl_connector=self.project_manager._kubectl_connector,
                progress_window_seconds=deletion_progress_window(),
            )
            if not gone:
                standing.append(name)
        deletion_results["operations"].append(
            {
                "type": "project_application_deletion_wait",
                "target": os.path.join(cluster, project_name),
                "status": "success" if not standing else "timeout",
                "standing": standing,
            }
        )
        if standing:
            message = (
                f"ArgoCD application(s) {', '.join(standing)} of project '{project_name}' are still in the cluster, "
                f"so the AppProject stays in the GitOps repository: removing it now would leave them pointing at an "
                f"AppProject that does not exist, and that stalls the sync of user-applications for every project"
            )
            logger.error(message)
            deletion_results["errors"].append(message)
            deletion_results["success"] = False
            return False
        return True

    async def _delete_project_argocd_folder(
        self, project_name: str, cluster: str, deletion_results: dict[str, Any], expect_folder: bool = False
    ) -> bool:
        """
        Delete the project's ArgoCD folder from the GitOps repository.

        The folder {cluster}/{project_name}/ contains the AppProject manifest,
        repository secret and kustomization.yaml. Deployment-level Application
        files are deleted per deployment, but without removing this folder the
        root application keeps re-applying the AppProject and repository secret
        (selfHeal), leaving orphaned resources behind after project deletion.

        Args:
            project_name: Name of the project
            cluster: Cluster name (e.g., "local", "odcn-production")
            deletion_results: Results dictionary to append operations/errors to
            expect_folder: True when the project had deployments on THIS cluster. The
                directory then OUGHT to be there and a missing one is an error.

        Returns True when the directory really left git in this round. Only then does the
        re-measurement wait for the AppProject to disappear from the cluster.

        A missing directory is two very different things. For a project without deployments on
        this cluster there never was one and "not found" is correct. For a project WITH
        deployments it ought to be there, and "not found" means it could not be found, not that
        it is absent. Without that distinction the deletion notes ``not_found``, carries on and
        throws the project file away, leaving an orphaned directory the root application keeps
        re-applying forever.
        """
        project_argocd_folder_rel = os.path.join(cluster, project_name)
        logger.info(f"Deleting project ArgoCD folder: {project_argocd_folder_rel}")

        try:
            gitops_workspace = await self.project_manager.get_argo_workspace()
            working_dir = await gitops_workspace.get_working_dir()
            project_argocd_folder = os.path.join(working_dir, project_argocd_folder_rel)

            # The DECISION belongs inside the lock, on the state reset_to_remote fetches
            # there: a cached clone can be arbitrarily old, and another task may have rewritten
            # this project directory meanwhile.
            async def _delete_project_directory() -> bool:
                if not os.path.exists(project_argocd_folder):
                    return False
                await asyncio.to_thread(shutil.rmtree, project_argocd_folder)
                return True

            commit_message = f"Delete ArgoCD resources for project '{project_name}'"
            verwijderd = await gitops_workspace.mutate_and_push(commit_message, _delete_project_directory)

            if verwijderd:
                deletion_results["operations"].append(
                    {
                        "type": "project_argocd_folder_deletion",
                        "target": project_argocd_folder_rel,
                        "status": "success",
                    }
                )
                logger.info(f"Deleted project ArgoCD folder: {project_argocd_folder_rel}")
                deletion_results["operations"].append(
                    {"type": "project_argocd_gitops_commit", "status": "success", "message": commit_message}
                )

                # Refresh the root application so the orphaned AppProject and
                # repository secret are pruned promptly
                argo_connector = create_argo_connector()
                await argo_connector.refresh_application("user-applications")
                return True
            elif expect_folder:
                # It was supposed to be there. Carrying on would throw the project file away
                # and leave the directory standing, which is the orphan nobody finds again.
                message = (
                    f"ArgoCD folder '{project_argocd_folder_rel}' was expected but not found in the GitOps "
                    f"repository; refusing to continue so the project file is not removed while its ArgoCD "
                    f"resources stay behind"
                )
                logger.error(message)
                deletion_results["operations"].append(
                    {
                        "type": "project_argocd_folder_deletion",
                        "target": project_argocd_folder_rel,
                        "status": "missing",
                    }
                )
                deletion_results["errors"].append(message)
                deletion_results["success"] = False
            else:
                deletion_results["operations"].append(
                    {
                        "type": "project_argocd_folder_deletion",
                        "target": project_argocd_folder_rel,
                        "status": "not_found",
                    }
                )
        except Exception as e:
            logger.exception(f"Error deleting project ArgoCD folder for {project_name}")
            deletion_results["errors"].append(f"Failed to delete project ArgoCD folder: {e}")
            deletion_results["success"] = False
        return False

    async def _cleanup_project_keycloak_realm(
        self,
        project_name: str,
        cluster: str,
        kc_config: dict[str, Any],
        deletion_results: dict[str, Any],
        only_if_present: bool = False,
    ) -> None:
        """Clean up the project-level Keycloak resources of a cluster: the project realm, the
        project admin in the master realm, the platform client in the RIG Platform realm, and
        the keycloak config entry in the project file.

        Called when the last deployment in a cluster is deleted.

        Args:
            kc_config: Keycloak config entry with host, realm, username and password.
        """
        from opi.connectors.keycloak import create_keycloak_connector
        from opi.utils.naming import generate_project_platform_client_id

        realm_name = kc_config["realm"]
        admin_username = kc_config["username"]
        keycloak_host = kc_config["host"]

        platform_client_id = generate_project_platform_client_id(project_name, cluster)

        logger.info(f"Cleaning up project Keycloak realm {realm_name} for cluster {cluster}")

        try:
            keycloak = await create_keycloak_connector(
                keycloak_url=keycloak_host,
                admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
            )

            if only_if_present and not await keycloak.realm_exists(realm_name):
                # Called with names derived from project + cluster rather than read from the
                # file. A project that never used Keycloak has nothing here, and reporting
                # three failed deletions for it would bury the failures that do matter.
                logger.info(f"No Keycloak realm '{realm_name}' present; nothing to clean up")
                return

            try:
                await keycloak.delete_realm(realm_name)
                logger.info(f"Deleted project realm {realm_name}")
                deletion_results["operations"].append(
                    {"type": "keycloak_realm_deletion", "target": realm_name, "status": "success"}
                )
            except Exception as e:
                logger.exception(f"Failed to delete realm {realm_name}")
                deletion_results["errors"].append(f"Realm deletion: {e}")

            try:
                deleted = await keycloak.delete_user_by_username("master", admin_username)
                if deleted:
                    logger.info(f"Deleted project admin {admin_username}")
                else:
                    logger.info(f"Project admin {admin_username} not found")
                deletion_results["operations"].append(
                    {
                        "type": "keycloak_user_deletion",
                        "target": admin_username,
                        "status": "success" if deleted else "not_found",
                    }
                )
            except Exception as e:
                logger.exception(f"Failed to delete user {admin_username}")
                deletion_results["errors"].append(f"User deletion: {e}")

            try:
                deleted = await keycloak.delete_client_by_client_id(settings.KEYCLOAK_DEFAULT_REALM, platform_client_id)
                if deleted:
                    logger.info(f"Deleted platform client {platform_client_id}")
                else:
                    logger.info(f"Platform client {platform_client_id} not found")
                deletion_results["operations"].append(
                    {
                        "type": "keycloak_platform_client_deletion",
                        "target": platform_client_id,
                        "status": "success" if deleted else "not_found",
                    }
                )
            except Exception as e:
                logger.exception("Failed to delete platform client")
                deletion_results["errors"].append(f"Platform client deletion: {e}")

            try:
                project_data = await self.project_manager.get_contents()
                # RC-5 B: keycloak connections live under the keycloak service config.
                view = Project(project_data)
                keycloak_list = view.get("services/keycloak/config/realms") or []

                # Remove entry matching this realm
                updated_list = [kc for kc in keycloak_list if kc.get("realm") != realm_name]

                if updated_list != keycloak_list:
                    view.set("services/keycloak/config/realms", updated_list)
                    # Central save: writes and commits as one locked operation. Do not write
                    # into the shared warm working copy without committing, or the change is
                    # swept up by whichever unrelated operation runs `git add -A` next.
                    await self.project_manager.save_and_commit_project(
                        project_data,
                        f"Remove keycloak config for realm {realm_name}",
                        enforce_validation=False,
                    )
                    logger.info(f"Removed keycloak config for realm {realm_name} from project.yaml")
                    deletion_results["operations"].append(
                        {
                            "type": "project_config_update",
                            "target": f"config.keycloak[{realm_name}]",
                            "status": "success",
                        }
                    )
            except Exception as e:
                logger.exception("Failed to update project config")
                deletion_results["errors"].append(f"Config update: {e}")

        except Exception as e:
            logger.exception("Error during realm cleanup")
            deletion_results["errors"].append(f"Realm cleanup: {e}")

    async def _cleanup_project_infrastructure(
        self,
        project_name: str,
        cluster: str,
        project_data: dict[str, Any],
        deletion_results: dict[str, Any],
        force: bool = False,
    ) -> None:
        """Clean up the infrastructure that is shared across every deployment, such as a
        dedicated PostgreSQL cluster or Redis instance.

        In order: the infrastructure ArgoCD folder out of the GitOps repo, a refresh of
        user-applications, the wait for the infrastructure Application to go, the
        infrastructure namespace, and the infrastructure manifests in the deployment repo.

        Args:
            force: Carry on through errors, and remove a finalizer when needed.
        """
        from opi.core.cluster_config import get_infrastructure_namespace
        from opi.services.services import ServiceType
        from opi.utils.naming import generate_infrastructure_manifest_path

        namespace_services = {
            ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
            ServiceType.NAMESPACE_REDIS.value,
        }

        project_services = project_data.get("services", [])
        # service_entry_name resolves all three entry formats. Matching on the raw dict
        # keys only saw the legacy single-key form, so a namespace service carrying
        # config went undetected here and its infrastructure was left behind on delete.
        # postgresql-database with scope: project also owns an infrastructure namespace
        # (RC-17), so include it or its dedicated cluster leaks on delete.
        uses_namespace_infrastructure = any(
            service_entry_name(entry) in namespace_services for entry in project_services
        ) or project_uses_dedicated_postgres(project_data)

        if not uses_namespace_infrastructure:
            logger.debug(
                f"Project '{project_name}' does not use namespace-specific services, skipping infrastructure deletion"
            )
            return

        logger.info(f"Cleaning up project-level infrastructure for project '{project_name}'")

        try:
            infra_app_name = generate_infrastructure_application_name(project_name)
            infra_argocd_folder_rel = generate_infrastructure_argocd_folder_path(cluster, project_name)
            logger.info(f"Deleting infrastructure ArgoCD folder: {infra_argocd_folder_rel}")

            gitops_workspace = await self.project_manager.get_argo_workspace()
            working_dir = await gitops_workspace.get_working_dir()
            infra_argocd_folder = os.path.join(working_dir, infra_argocd_folder_rel)

            async def _delete_infrastructure_directory() -> bool:
                if not os.path.exists(infra_argocd_folder):
                    return False
                await asyncio.to_thread(shutil.rmtree, infra_argocd_folder)
                return True

            commit_message = f"Delete infrastructure ArgoCD application for project '{project_name}'"
            verwijderd = await gitops_workspace.mutate_and_push(commit_message, _delete_infrastructure_directory)

            if verwijderd:
                deletion_results["operations"].append(
                    {
                        "type": "infrastructure_argocd_folder_deletion",
                        "target": infra_argocd_folder_rel,
                        "status": "success",
                    }
                )
                logger.info(f"Deleted infrastructure ArgoCD folder: {infra_argocd_folder_rel}")
                deletion_results["operations"].append(
                    {"type": "infrastructure_gitops_commit", "status": "success", "message": commit_message}
                )
            else:
                deletion_results["operations"].append(
                    {
                        "type": "infrastructure_argocd_folder_deletion",
                        "target": infra_argocd_folder_rel,
                        "status": "not_found",
                    }
                )

            argo_connector = create_argo_connector()
            refresh_success = await argo_connector.refresh_application("user-applications")
            if refresh_success:
                deletion_results["operations"].append(
                    {
                        "type": "infrastructure_argocd_refresh",
                        "target": "user-applications",
                        "status": "success",
                    }
                )
                logger.info("Refreshed user-applications after infrastructure GitOps deletion")

            infra_app_deleted = False
            infra_app_exists = await argo_connector.application_exists(infra_app_name)
            if infra_app_exists:
                logger.info(f"Waiting for infrastructure Application {infra_app_name} to be deleted")
                await self._terminate_application_operation(infra_app_name, deletion_results)
                deletion_complete = await argo_connector.wait_for_application_deletion(
                    infra_app_name,
                    max_retries=20,
                    kubectl_connector=self.project_manager._kubectl_connector,
                    progress_window_seconds=deletion_progress_window(),
                )

                if deletion_complete:
                    deletion_results["operations"].append(
                        {
                            "type": "infrastructure_app_deletion_wait",
                            "target": infra_app_name,
                            "status": "success",
                        }
                    )
                    logger.info(f"Infrastructure Application {infra_app_name} successfully deleted")
                    infra_app_deleted = True
                else:
                    # Timeout - in force mode, try to remove finalizers
                    finalizer_removed = False
                    if force:
                        logger.warning(
                            f"Infrastructure Application {infra_app_name} deletion timed out - "
                            "force mode: deleting its resources, then removing finalizers"
                        )
                        outcome = await self._force_delete_or_await_cascade(
                            infra_app_name, deletion_results, argo_connector
                        )
                        infra_app_deleted = outcome.application_gone
                        finalizer_removed = outcome.finalizer_removed

                    deletion_results["operations"].append(
                        {
                            "type": "infrastructure_app_deletion_wait",
                            "target": infra_app_name,
                            "status": "timeout",
                            "finalizer_removed": finalizer_removed,
                        }
                    )
                    logger.warning(f"Infrastructure Application {infra_app_name} deletion timed out")
            else:
                deletion_results["operations"].append(
                    {
                        "type": "infrastructure_app_deletion_wait",
                        "target": infra_app_name,
                        "status": "not_found",
                    }
                )
                infra_app_deleted = True

            infra_namespace = get_infrastructure_namespace(cluster, project_name)

            if not infra_app_deleted and not force:
                deletion_results["operations"].append(
                    {
                        "type": "infrastructure_namespace_deletion",
                        "target": infra_namespace,
                        "status": "skipped",
                        "reason": "Infrastructure ArgoCD app not confirmed deleted",
                    }
                )
                deletion_results["errors"].append(
                    f"Infrastructure namespace '{infra_namespace}' not deleted: "
                    f"ArgoCD app '{infra_app_name}' not confirmed deleted. Use force=true."
                )
                logger.warning(
                    f"Skipping infrastructure namespace deletion - ArgoCD app {infra_app_name} not confirmed deleted"
                )
            else:
                if not infra_app_deleted:
                    logger.warning(
                        f"Force mode: deleting infrastructure namespace {infra_namespace} "
                        "even though ArgoCD app status is uncertain"
                    )
                logger.info(f"Deleting infrastructure namespace: {infra_namespace}")
                infra_namespace_deleted = await self.project_manager._kubectl_connector.delete_namespace(
                    infra_namespace
                )

                if infra_namespace_deleted:
                    deletion_results["operations"].append(
                        {
                            "type": "infrastructure_namespace_deletion",
                            "target": infra_namespace,
                            "status": "success",
                        }
                    )
                    logger.info(f"Successfully deleted infrastructure namespace: {infra_namespace}")
                else:
                    # As above: --ignore-not-found makes a genuinely absent namespace
                    # return True, so a False is a real failure -- surface it rather than
                    # calling it "not_found" and leaking the infrastructure namespace.
                    deletion_results["operations"].append(
                        {
                            "type": "infrastructure_namespace_deletion",
                            "target": infra_namespace,
                            "status": "error",
                            "error": "kubectl delete namespace failed (see logs); namespace left behind",
                        }
                    )
                    deletion_results["errors"].append(
                        f"Failed to delete infrastructure namespace '{infra_namespace}' (see logs)."
                    )
                    deletion_results["success"] = False
                    logger.error(f"Failed to delete infrastructure namespace {infra_namespace}; it was left behind")

            repositories = project_data.get("repositories", [])
            if repositories:
                main_repo = repositories[0]
                repo_config = main_repo
                workspace = await self.project_manager.get_deployment_workspace("infrastructure", repo_config)

                repo_path = repo_config.get("path", "")
                infra_manifest_path = generate_infrastructure_manifest_path(cluster, project_name, repo_path)
                logger.info(f"Deleting infrastructure manifests folder: {infra_manifest_path}")

                commit_message = f"Delete infrastructure manifests for project '{project_name}'"
                if await workspace.remove_and_push(infra_manifest_path, commit_message):
                    deletion_results["operations"].append(
                        {
                            "type": "infrastructure_manifest_deletion",
                            "target": infra_manifest_path,
                            "status": "success",
                        }
                    )
                    logger.info(f"Deleted infrastructure manifests folder: {infra_manifest_path}")
                    deletion_results["operations"].append(
                        {
                            "type": "infrastructure_manifest_commit",
                            "status": "success",
                            "message": commit_message,
                        }
                    )
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "infrastructure_manifest_deletion",
                            "target": infra_manifest_path,
                            "status": "not_found",
                        }
                    )

            logger.info(f"Infrastructure cleanup completed for project '{project_name}'")

        except Exception as e:
            deletion_results["operations"].append(
                {
                    "type": "infrastructure_deletion",
                    "status": "error",
                    "error": str(e),
                }
            )
            deletion_results["errors"].append(f"Error deleting infrastructure: {e}")
            logger.exception(f"Error deleting infrastructure for project '{project_name}'")

    async def _delete_project_level_folder(
        self,
        project_name: str,
        cluster: str,
        project_data: dict[str, Any],
        deployments: list[dict[str, Any]],
        deletion_results: dict[str, Any],
    ) -> None:
        """Delete ``<cluster>/<project>/_project`` from the deployments repo.

        The project level hangs on the project, so the per-deployment cleanup never touches
        it. Which repository carries the directory is decided by ``project_level_deployment``,
        the same function the writer and the ArgoCD application use.
        """
        owner = project_level_deployment(deployments)
        repository_name = owner.get("repository") if owner else None
        repo_config = next(
            (r for r in project_data.get("repositories", []) or [] if r.get("name") == repository_name), None
        )
        if not repo_config:
            logger.debug(f"Geen repository voor het projectniveau van '{project_name}'; niets te verwijderen")
            return

        try:
            workspace = await self.project_manager.get_deployment_workspace(str(repository_name), repo_config)
            folder = generate_project_level_manifest_path(cluster, project_name, repo_config.get("path", ""))
            if not await workspace.remove_and_push(folder, f"Delete project level of project '{project_name}'"):
                deletion_results["operations"].append(
                    {"type": "project_level_folder_deletion", "target": folder, "status": "not_found"}
                )
                return
            deletion_results["operations"].append(
                {"type": "project_level_folder_deletion", "target": folder, "status": "success"}
            )
            logger.info(f"Deleted project level folder: {folder}")
        except Exception as e:
            deletion_results["errors"].append(f"Error deleting project level folder: {e}")
            logger.exception("Error deleting project level folder")

    async def _delete_project_namespaces(
        self,
        project_name: str,
        cluster: str,
        deployments: list[dict[str, Any]],
        deletion_results: dict[str, Any],
    ) -> None:
        """Delete the namespaces now that no deployment points at them (cases 5 and 6.1-1)."""
        kubectl = self.project_manager._kubectl_connector
        for base_namespace in sorted({dep.get("namespace") or project_name for dep in deployments}):
            namespace = get_prefixed_namespace(cluster, base_namespace)
            if await kubectl.namespace_absent(namespace) is True:
                deletion_results["operations"].append(
                    {"type": "project_namespace_deletion", "target": namespace, "status": "not_found"}
                )
                continue

            logger.info(f"Deleting namespace {namespace} of project '{project_name}'")
            if not await kubectl.delete_namespace(namespace):
                deletion_results["operations"].append(
                    {"type": "project_namespace_deletion", "target": namespace, "status": "error"}
                )
                deletion_results["errors"].append(
                    f"Failed to delete namespace '{namespace}' (see logs for the kubectl error)."
                )
                deletion_results["success"] = False
                continue

            # The Kubernetes API is the truth: an accepted delete does not say the namespace is
            # gone, because its finalizers may still be running.
            gone = await kubectl.wait_for_namespace_deletion(namespace)
            deletion_results["operations"].append(
                {
                    "type": "project_namespace_deletion",
                    "target": namespace,
                    "status": "success" if gone else "timeout",
                }
            )
            if not gone:
                deletion_results["errors"].append(f"Namespace '{namespace}' was not confirmed gone after its delete")
                deletion_results["success"] = False

    async def _judge_and_delete_project_file(
        self,
        project_name: str,
        cluster: str,
        deployments: list[dict[str, Any]],
        deletion_results: dict[str, Any],
        appprojects_left_git: bool = False,
    ) -> None:
        """Measure the layers again, and only delete the project file when nothing is left.

        The run's errors stay in the result as an explanation; they decide nothing any more.
        When the measurement could not run, the run decides after all: unknown is proof in
        neither direction.
        """
        step = self._begin_step(steps.DELETE_VERIFY)
        errors_before = len(deletion_results["errors"])
        appprojects = [
            generate_argocd_appproject_prefix(project_name, dep.get("namespace") or project_name) for dep in deployments
        ]
        if appprojects_left_git:
            # ArgoCD prunes the AppProject asynchronously once its manifest is out of git;
            # without this wait the re-measurement reads the intermediate state and fails a
            # delete that was clean seconds later.
            kubectl = self.project_manager._kubectl_connector
            argo_namespace = get_argo_namespace(cluster)
            for appproject_name in appprojects:
                await kubectl.wait_for_resource_absence("appproject", appproject_name, argo_namespace)
        remaining = await self._measure_remaining(
            cluster,
            applications=[generate_argocd_application_name(project_name, str(dep.get("name"))) for dep in deployments]
            + [generate_argocd_project_application_name(project_name)],
            appprojects=appprojects,
            namespaces=[get_prefixed_namespace(cluster, dep.get("namespace") or project_name) for dep in deployments],
        )
        if remaining.decides:
            deletion_results["success"] = not remaining.present
            for leftover in remaining.present:
                deletion_results["errors"].append(
                    f"Project '{project_name}' is not fully deleted: {leftover} still stands"
                )
        deletion_results["operations"].append(
            {
                "type": "deletion_remeasurement",
                "target": f"project '{project_name}'",
                "status": ("incomplete" if remaining.present else "clean") if remaining.decides else "unreadable",
                "remaining": remaining.present,
                "unreadable": remaining.unreadable,
            }
        )
        self._end_step(step, self._new_errors(deletion_results, errors_before))

        if not deletion_results["success"]:
            logger.warning(
                f"Keeping the project file of '{project_name}': "
                f"{', '.join(remaining.present) or 'the deletion reported errors'}"
            )
            deletion_results["operations"].append(
                {
                    "type": "project_file_deletion",
                    "target": project_name,
                    "status": "skipped",
                    "reason": "the deletion is not finished, so the project file stays as the input for a retry",
                }
            )
            return

        logger.info(f"Everything of project {project_name} is gone, now deleting its project file")
        commit_message = f"Delete project '{project_name}' - removed project file after deployment cleanup"
        step = self._begin_step(steps.PROJECT_FILE_DELETE)
        errors_before = len(deletion_results["errors"])
        delete_result = await self._delete_project_file(project_name, commit_message)
        deletion_results["operations"].extend(delete_result["operations"])
        deletion_results["errors"].extend(delete_result["errors"])
        if not delete_result["success"]:
            deletion_results["success"] = False
        self._end_step(step, self._new_errors(deletion_results, errors_before))

    async def delete_project(self, project_name: str, force: bool = False) -> dict[str, Any]:
        """
        Delete a project by first deleting all deployments, then cleaning up project-level resources.

        This method implements the project deletion logic:
        1. Validate no deployments exist on other clusters
        2. Delete all deployments on the current cluster (deployment-level resources only)
        3. Clean up project-level infrastructure (namespace-specific PostgreSQL/Redis)
        4. Clean up project Keycloak realm(s)
        5. Remove the project folders from the deployments and GitOps repos, the
           Applications before the AppProject, and the namespaces of this project
        6. Measure the layers again, and delete the project file only when nothing is left
        7. Clean up subdomains and in-memory state

        Args:
            project_name: Name of the project to delete
            force: Carry on through errors and clean up stuck resources, for a previous
                   deletion that failed partially. It does NOT remove the project file while
                   anything still stands: that is the one irreversible step, and the file is
                   the input of every new attempt.

        Returns:
            Dictionary containing deletion results and status

        Raises:
            HTTPException: If critical operations fail or deployments exist on other clusters
                          (unless force=True, which continues on most errors)
        """
        deletion_results = {
            "project": project_name,
            "operations": [],
            "success": True,
            "errors": [],
            "deployment_deletions": {},
            "remaining_deployments": [],
            "force_mode": force,
        }

        if force:
            logger.info(f"Force mode enabled for project deletion: {project_name}")

        project = get_project_store().get(project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in project service")
        self.project_manager._project_file_relative_path = f"projects/{project.filename}"

        try:
            project_data = await self.project_manager.get_contents()

            current_cluster = settings.CLUSTER_MANAGER
            current_cluster_deployments = await self.project_manager.get_deployments(cluster_filter=True)
            all_deployments = await self.project_manager.get_deployments(cluster_filter=False)
            other_cluster_deployments = [d for d in all_deployments if d.get("cluster") != current_cluster]

            logger.info(
                f"Project {project_name} has {len(current_cluster_deployments)} deployments on current cluster '{current_cluster}' "
                f"and {len(other_cluster_deployments)} deployments on other clusters"
            )

            if other_cluster_deployments:
                other_clusters = {dep.get("cluster") for dep in other_cluster_deployments}
                deletion_results["remaining_deployments"] = [
                    {"name": dep.get("name"), "cluster": dep.get("cluster")} for dep in other_cluster_deployments
                ]
                deletion_results["success"] = False
                deletion_results["errors"].append(
                    f"Cannot delete project '{project_name}' because it has deployments on other clusters: {', '.join(other_clusters)}. "
                    f"Please delete those deployments first or switch to the appropriate cluster manager."
                )

                deletion_results["operations"].append(
                    {
                        "type": "project_deletion_validation",
                        "status": "blocked",
                        "reason": "deployments_on_other_clusters",
                        "other_clusters": list(other_clusters),
                        "remaining_deployments": deletion_results["remaining_deployments"],
                    }
                )

                logger.warning(
                    f"Project deletion blocked - {project_name} has deployments on other clusters: {other_clusters}"
                )
                return deletion_results

            #
            # Every deployment of this round knows the names of the others, so the gate on the
            # namespace does not take a sibling that is going away just as hard for one that
            # stays (case 5).
            leaving = {str(dep.get("name")) for dep in current_cluster_deployments if dep.get("name")}
            for deployment in current_cluster_deployments:
                deployment_name = deployment.get("name")
                logger.info(f"Deleting deployment {deployment_name} from project {project_name}")

                try:
                    deployment_deletion_result = await self.delete_deployment(
                        project_name, deployment_name, force=force, leaving=leaving
                    )
                    deletion_results["deployment_deletions"][deployment_name] = deployment_deletion_result
                    deletion_results["operations"].extend(deployment_deletion_result["operations"])

                    if deployment_deletion_result["success"]:
                        deletion_results["operations"].append(
                            {
                                "type": "deployment_deletion",
                                "deployment": deployment_name,
                                "cluster": current_cluster,
                                "status": "success",
                            }
                        )
                        logger.info(f"Successfully deleted deployment {deployment_name}")
                    else:
                        deletion_results["errors"].extend(deployment_deletion_result["errors"])
                        deletion_results["operations"].append(
                            {
                                "type": "deployment_deletion",
                                "deployment": deployment_name,
                                "cluster": current_cluster,
                                "status": "partial" if force else "failed",
                                "errors": deployment_deletion_result["errors"],
                            }
                        )
                        # With force too: a deployment that failed does not make the project
                        # deletion a success. force means "do not stop at the first error", not
                        # "call a half deletion done".
                        deletion_results["success"] = False
                        logger.error(
                            f"Failed to delete deployment {deployment_name}: {deployment_deletion_result['errors']}"
                        )

                except Exception as e:
                    error_msg = f"Error deleting deployment {deployment_name}: {e}"
                    deletion_results["errors"].append(error_msg)
                    deletion_results["operations"].append(
                        {
                            "type": "deployment_deletion",
                            "deployment": deployment_name,
                            "cluster": current_cluster,
                            "status": "error",
                            "error": str(e),
                        }
                    )
                    deletion_results["success"] = False
                    logger.exception(error_msg)

            if deletion_results["success"] or force:
                step = self._begin_step(steps.PROJECT_INFRA_CLEANUP)
                errors_before = len(deletion_results["errors"])
                await self._cleanup_project_infrastructure(
                    project_name, current_cluster, project_data, deletion_results, force
                )
                self._end_step(step, self._new_errors(deletion_results, errors_before))

            if deletion_results["success"] or force:
                step = self._begin_step(steps.KEYCLOAK_REALM_DELETE)
                errors_before = len(deletion_results["errors"])
                kc_config = await self.project_manager._keycloak_manager._get_project_keycloak_config_for_cluster(
                    current_cluster
                )
                if kc_config:
                    logger.info(f"Cleaning up project Keycloak realm for cluster {current_cluster}")
                    await self._cleanup_project_keycloak_realm(
                        project_name=project_name,
                        cluster=current_cluster,
                        kc_config=kc_config,
                        deletion_results=deletion_results,
                    )
                else:
                    # No config entry does not mean nothing was created. The realm and the
                    # master-realm admin user are named deterministically from project and
                    # cluster, so they can be removed without reading the file. Skipping here
                    # leaves both behind silently, and an orphaned admin account that still
                    # carries an OTP credential should not stay behind because a config block
                    # went missing.
                    await self._cleanup_project_keycloak_realm(
                        project_name=project_name,
                        cluster=current_cluster,
                        kc_config={
                            "realm": generate_project_realm_name(project_name, current_cluster),
                            "username": generate_project_admin_username(project_name, current_cluster),
                            "host": self.project_manager._keycloak_manager._get_keycloak_url_for_cluster(
                                current_cluster
                            ),
                        },
                        deletion_results=deletion_results,
                        only_if_present=True,
                    )
                self._end_step(step, self._new_errors(deletion_results, errors_before))

            # Steps 4.65 and 4.7 together are one visible step. The gates keep measuring per
            # part, so the step begins as soon as the first part runs and cannot report in
            # afterwards without the work having run.
            argo_step: str | None = None
            errors_before_argo = len(deletion_results["errors"])
            appprojects_left_git = False

            if deletion_results["success"] or force:
                argo_step = self._begin_step(steps.PROJECT_ARGOCD_DELETE)
                await self._delete_project_level_folder(
                    project_name, current_cluster, project_data, current_cluster_deployments, deletion_results
                )

            # Step 4.7: Delete the project's ArgoCD folder (AppProject, repository secret, kustomization)
            # from the GitOps repo, so the root application prunes these resources.
            # This folder also holds the project-level application ({project}-project), and
            # that one goes first, in a commit of its own: the AppProject may only leave the
            # folder once the cluster reports no Application points at it any more.
            if deletion_results["success"] or force:
                try:
                    applications_released = await self._release_project_applications(
                        project_name, current_cluster, deletion_results
                    )
                except Exception as e:
                    self._end_step(argo_step, str(e))
                    raise
                if applications_released:
                    appprojects_left_git = await self._delete_project_argocd_folder(
                        project_name,
                        current_cluster,
                        deletion_results,
                        expect_folder=len(current_cluster_deployments) > 0,
                    )
            self._end_step(argo_step, self._new_errors(deletion_results, errors_before_argo))

            if deletion_results["success"] or force:
                step = self._begin_step(steps.NAMESPACES_DELETE)
                errors_before = len(deletion_results["errors"])
                await self._delete_project_namespaces(
                    project_name, current_cluster, current_cluster_deployments, deletion_results
                )
                self._end_step(step, self._new_errors(deletion_results, errors_before))

            # Steps 4.9 and 5: the re-measurement decides the outcome, and with it whether the
            # project file may go. That file is the input of every later round and of the orphan
            # sweep, so while anything still stands it has to stay: otherwise nobody can finish
            # the deletion or even report it (cases 2c and 5). force does not override that,
            # because that is how a namespace was kept with no project file next to it.
            if len(current_cluster_deployments) > 0:
                await self._judge_and_delete_project_file(
                    project_name,
                    current_cluster,
                    current_cluster_deployments,
                    deletion_results,
                    appprojects_left_git=appprojects_left_git,
                )
            else:
                logger.info(f"No deployments found on current cluster '{current_cluster}' for project {project_name}")
                deletion_results["operations"].append(
                    {
                        "type": "project_status_check",
                        "status": "no_deployments_on_current_cluster",
                        "message": f"Project has no deployments on current cluster '{current_cluster}'",
                    }
                )

                await self._cleanup_orphaned_argocd_resources(project_name, deletion_results)

                commit_message = f"Delete project '{project_name}' - no deployments remaining"
                step = self._begin_step(steps.PROJECT_FILE_DELETE)
                errors_before = len(deletion_results["errors"])
                delete_result = await self._delete_project_file(project_name, commit_message)

                deletion_results["operations"].extend(delete_result["operations"])
                deletion_results["errors"].extend(delete_result["errors"])
                if not delete_result["success"]:
                    deletion_results["success"] = False
                self._end_step(step, self._new_errors(deletion_results, errors_before))

            step = self._begin_step(steps.SUBDOMAINS_CLEANUP)
            errors_before = len(deletion_results["errors"])
            try:
                subdomain_connector = SubdomainConnector()
                deleted_subdomains = await subdomain_connector.delete_by_project(project_name)
                if deleted_subdomains:
                    deletion_results["operations"].append(
                        {
                            "type": "subdomain_cleanup",
                            "target": f"project '{project_name}'",
                            "status": "success",
                            "count": deleted_subdomains,
                            "message": f"Deleted {deleted_subdomains} subdomain registration(s)",
                        }
                    )
                    logger.info(f"Deleted {deleted_subdomains} subdomain registration(s) for project '{project_name}'")
                else:
                    logger.debug(f"No subdomain registrations found for project '{project_name}'")
            except Exception as e:
                error_msg = f"Error cleaning up subdomain registrations: {e}"
                deletion_results["errors"].append(error_msg)
                deletion_results["operations"].append({"type": "subdomain_cleanup", "status": "error", "error": str(e)})
                logger.warning(error_msg)
                # Don't fail the deletion for subdomain cleanup errors
            self._end_step(step, self._new_errors(deletion_results, errors_before))

            # Final step: confirm the project is gone from the read cache.
            #
            # store.delete() already evicted it as part of the deletion, so this only
            # reports the outcome. Evicting from the cache directly would be a cache
            # mutation outside the store, which is how the cache and git drift apart.
            if deletion_results["success"]:
                try:
                    removed = get_project_store().get(project_name) is None
                    if removed:
                        deletion_results["operations"].append(
                            {
                                "type": "in_memory_cleanup",
                                "target": f"project '{project_name}'",
                                "status": "success",
                                "message": "Removed project from in-memory database",
                            }
                        )
                        logger.info(f"Successfully removed project '{project_name}' from in-memory database")
                    else:
                        deletion_results["operations"].append(
                            {
                                "type": "in_memory_cleanup",
                                "target": f"project '{project_name}'",
                                "status": "not_found",
                                "message": "Project not found in in-memory database",
                            }
                        )
                        logger.info(f"Project '{project_name}' was not found in in-memory database")
                except Exception as e:
                    error_msg = f"Error removing project from in-memory database: {e}"
                    deletion_results["errors"].append(error_msg)
                    deletion_results["operations"].append(
                        {"type": "in_memory_cleanup", "status": "error", "error": str(e)}
                    )
                    logger.warning(error_msg)

            return deletion_results

        except HTTPException:
            raise
        except Exception as e:
            deletion_results["success"] = False
            deletion_results["errors"].append(f"Unexpected error during project deletion: {e}")
            logger.exception(f"Unexpected error during project deletion for {project_name}")
            return deletion_results

    async def _delete_project_file(self, project_name: str, commit_message: str) -> dict[str, Any]:
        result = {"success": True, "operations": [], "errors": []}

        try:
            project = get_project_store().get(project_name)
            if not project:
                result["errors"].append(f"Project '{project_name}' not found in project service")
                result["success"] = False
                return result
            project_file_path = f"projects/{project.filename}"

            # Delete through the store rather than `git rm` + commit on the shared warm
            # copy. `git rm` stages the removal immediately, so if the commit or push then
            # failed the deletion stayed staged and the next commit for an unrelated
            # project carried it to the remote -- silently deleting this project file under
            # someone else's commit message. The store builds the deletion as its own
            # commit and rolls the ref back if the push fails.
            await get_project_store().delete(project_name, message=commit_message, actor=get_actor())

            result["operations"].append(
                {
                    "type": "project_file_deletion",
                    "target": project_file_path,
                    "status": "success",
                    "message": commit_message,
                }
            )
            logger.info(f"Successfully deleted project file: {project_file_path}")

        except Exception as e:
            error_msg = f"Error deleting project file: {e}"
            result["errors"].append(error_msg)
            result["operations"].append({"type": "project_file_deletion", "status": "error", "error": str(e)})
            result["success"] = False
            logger.exception(error_msg)

        return result

    async def delete_deployment(
        self,
        project_name: str,
        deployment_name: str,
        force: bool = False,
        leaving: Collection[str] = (),
    ) -> dict[str, Any]:
        """Remove a deployment that a caller named, with the entry still in the project file.

        The one adapter of :func:`remove_deployment` that still answers the dictionary
        ``delete_project`` and the API routes read.

        Raises:
            HTTPException: 404 when the project or the deployment is not there, 500 when a
                step broke off the removal. Under ``force`` nothing is raised: the result
                carries the errors instead, so a project deletion and the nightly cleaner
                walk every deployment.
        """
        request = DeploymentRemoval(
            project_name=project_name,
            deployment_name=deployment_name,
            trigger=RemovalTrigger.EXPLICIT,
            force=force,
            leaving=leaving,
        )
        try:
            return (await remove_deployment(request, self)).details
        except (ProjectNotFoundError, DeploymentNotFoundError) as absent:
            raise HTTPException(status_code=404, detail=str(absent)) from absent
        except DeploymentRemovalError as e:
            raise HTTPException(status_code=500, detail=f"Critical error during deployment deletion: {e!s}") from e

    # The service-type to manager-key mapping lives on each provider as
    # `cleanup_manager_key`; this only resolves a key to its manager instance, and is invoked
    # via RemovalContext.get_manager.
    async def _get_manager_for_service(self, manager_key: ManagerKey) -> Any:
        """Resolve the manager instance for a given manager key.

        Database is special (an async ensure); the rest are plain attributes, so a dict lookup
        does. The enum makes the set exhaustive at type-check time, so a bad key is a pyright
        error and not a teardown-time crash.
        """
        if manager_key is ManagerKey.DATABASE:
            return await self.project_manager._ensure_database_manager()
        return {
            ManagerKey.MINIO: self.project_manager._minio_manager,
            ManagerKey.SEAWEEDFS: self.project_manager._seaweedfs_manager,
            ManagerKey.REDIS: self.project_manager._redis_manager,
            ManagerKey.KEYCLOAK: self.project_manager._keycloak_manager,
            ManagerKey.PVC: self.project_manager._pvc_manager,
            ManagerKey.MAIL: self.project_manager._mail_manager,
        }[manager_key]

    async def cleanup_removed_services_from_yaml_change(
        self,
        project_name: str,
        previous_yaml: dict[str, Any],
        current_yaml: dict[str, Any],
        marked_for_deletion_service: MarkedForDeletionService | None = None,
    ) -> dict[str, Any]:
        """Detect services removed from surviving deployments and clean them up.

        For each deployment that exists in both the previous and current YAML,
        this compares service usage.  If a service was used before but is no
        longer used, the corresponding manager's ``handle_service_removal()``
        is called.

        Args:
            project_name: Name of the project.
            previous_yaml: The previous project YAML.
            current_yaml: The current project YAML.
            marked_for_deletion_service: Optional service for deferred deletion
                of persistent resources.

        Returns:
            Aggregated result dict with per-deployment, per-service results.
        """
        results: dict[str, Any] = {
            "project": project_name,
            "trigger": "service_removal_detection",
            "deployments_checked": 0,
            "services_removed": 0,
            "service_results": [],
            "errors": [],
            "success": True,
        }

        previous_deployments = {
            dep["name"]: dep for dep in previous_yaml.get("deployments", []) if isinstance(dep, dict) and "name" in dep
        }
        current_deployments = {
            dep["name"]: dep for dep in current_yaml.get("deployments", []) if isinstance(dep, dict) and "name" in dep
        }

        surviving = set(previous_deployments) & set(current_deployments)
        results["deployments_checked"] = len(surviving)

        if not surviving:
            logger.debug("No surviving deployments to check for service removal")
            return results

        cleanable_services = ServiceAdapter.get_cleanable_service_types()

        file_handler = self.project_manager._project_file_handler

        for dep_name in sorted(surviving):
            prev_dep_data = previous_deployments[dep_name]

            for svc_type in cleanable_services:
                svc_values = [svc_type.value]
                # Cleanup is dispatched through the provider registry: the provider resolves
                # the manager by key and delegates handle_service_removal. Service types that
                # share a manager are therefore checked once per manager per deployment.
                provider = get_service(svc_type)
                if provider.cleanup_manager_key is None:
                    continue

                was_used = file_handler.deployment_uses_service(previous_yaml, dep_name, svc_values)
                still_used = file_handler.deployment_uses_service(current_yaml, dep_name, svc_values)

                if was_used and not still_used:
                    logger.info(
                        "Service '%s' removed from deployment '%s' in project '%s'",
                        svc_type.value,
                        dep_name,
                        project_name,
                    )
                    results["services_removed"] += 1

                    try:
                        svc_result = await provider.handle_service_removal(
                            RemovalContext(
                                project_name=project_name,
                                deployment_name=dep_name,
                                deployment_data=prev_dep_data,
                                project_data=previous_yaml,
                                marked_for_deletion_service=marked_for_deletion_service,
                                get_manager=self._get_manager_for_service,
                            )
                        )
                        results["service_results"].append(svc_result)
                        if svc_result.get("errors"):
                            results["errors"].extend(svc_result["errors"])
                    except Exception as e:
                        error_msg = f"Error cleaning up {svc_type.value} for deployment {dep_name}: {e}"
                        results["errors"].append(error_msg)
                        logger.exception(error_msg)

        results["success"] = len(results["errors"]) == 0

        if results["services_removed"] > 0:
            logger.info(
                "Service removal cleanup for project '%s': %d service(s) removed across %d deployment(s)",
                project_name,
                results["services_removed"],
                results["deployments_checked"],
            )

        return results
