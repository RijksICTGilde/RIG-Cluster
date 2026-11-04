"""Project deletion manager for handling project and deployment deletion operations."""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from fastapi import HTTPException

from opi.connectors import create_argo_connector
from opi.core.actor import get_actor
from opi.core.cluster_config import get_argo_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.services import ServiceAdapter, ServiceType
from opi.services.catalog.base import RemovalContext
from opi.services.persistence.subdomain_registry import SubdomainConnector
from opi.services.postgres_scope import project_uses_dedicated_postgres
from opi.services.project import Project
from opi.services.project_store import get_project_store
from opi.services.registry import get_service
from opi.services.services import service_entry_name
from opi.services.services_enums import ManagerKey
from opi.utils.argocd_tracking import LABEL_VALUE_MAX, may_be_cut_from
from opi.utils.naming import generate_project_admin_username, generate_project_realm_name
from opi.utils.rendered_kinds import rendered_kinds, rendered_types

if TYPE_CHECKING:
    from collections.abc import Collection

    from opi.connectors.argo import ArgoConnector
    from opi.services.marked_for_deletion_service import MarkedForDeletionService
    from opi.utils.argocd_tracking import TrackedResource
from opi.utils.naming import (
    generate_argocd_application_name,
    generate_argocd_appproject_prefix,
    generate_argocd_project_application_name,
    generate_backup_prefix,
    generate_deployment_manifest_path,
    generate_gitops_argocd_application_path,
    generate_infrastructure_application_name,
    generate_infrastructure_argocd_folder_path,
    generate_project_level_manifest_path,
    get_output_filename_from_template,
)
from opi.utils.project_utils import project_level_deployment

logger = logging.getLogger(__name__)

#: Hoeveel de wachten van EEN taak samen mogen doorlopen ZOLANG de cascade zichtbaar
#: vordert. Vordert er niets meer, dan stopt de wacht op de stilstand en niet op deze grens.
#:
#: Een budget per taak en niet per wacht, omdat twee van de drie aanroepers in een lus over
#: de deployments staan: per wacht zou een project met drie deployments aan puur wachten al
#: boven TASK_WORKER_MAX_DURATION (1800s) uitkomen, en die grens kapt de handler HARD af,
#: dus zonder force-route en zonder resultaat. 600s plus de gewone vensters van de overige
#: wachten (200s per deployment, 60s voor de infra-Application) laat ruimte voor de
#: force-route en het gitwerk daarna.
#:
#: Het budget wordt op de KLOK afgerekend en niet op de vensters die zijn uitgedeeld. Dat is
#: nodig omdat een poll meer kost dan zijn retry_delay: de inventaris van de doelnamespace was
#: op de sandbox 1,8s op een namespace met 271 resources. Een wacht kan zijn eigen venster in
#: wandkloktijd dus overschrijden, en dat gaat dan van de volgende wacht af.
DELETION_PROGRESS_BUDGET_SECONDS = 600

#: Het budget loopt per asyncio-taak, en dat is precies een taak van de task worker: die
#: voert elke handler als eigen taak uit. Een parameter zou langs drie aanroepers en twee
#: lussen in project_manager moeten reizen die er zelf niets mee doen.
_DELETION_PROGRESS_DEADLINE: ContextVar[float | None] = ContextVar("deletion_progress_deadline", default=None)


def deletion_progress_window() -> int | None:
    """Wat deze wacht nog van het voortgangsbudget van zijn taak mag gebruiken.

    ``None`` betekent dat het budget op is. Die wacht houdt dan het gewone venster en loopt
    dus netjes rood af, met de force-route erachter, in plaats van door de harde taakgrens
    midden in de delete afgekapt te worden.
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


def parse_retention_period_hours(value: str | None) -> int:
    """Parse a data-retention-period value into hours.

    Accepted formats: ``<number>h`` (hours) or ``<number>d`` (days).
    Returns 0 for ``None``, empty strings, or ``0h``/``0d``.
    Maximum is 7 days (168 hours).

    Raises:
        ValueError: If the format is invalid or exceeds maximum.
    """
    if not value:
        return 0

    value = value.strip().lower()
    if not value:
        return 0

    if value.endswith("d"):
        days = int(value[:-1])
        hours = days * 24
    elif value.endswith("h"):
        hours = int(value[:-1])
    else:
        raise ValueError(
            f"Invalid data-retention-period format: '{value}'. Use '<number>h' or '<number>d' (e.g., '0h', '3d')."
        )

    if hours < 0:
        raise ValueError(f"data-retention-period cannot be negative: '{value}'")
    if hours > 168:
        raise ValueError(f"data-retention-period cannot exceed 7 days (168 hours): '{value}'")

    return hours


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


def _namespace_used_by_others(
    deployments: list[dict[str, Any]],
    deployment_name: str,
    cluster: str | None,
    base_namespace: str | None,
    leaving: Collection[str] = (),
) -> bool:
    """Of een deployment die BLIJFT STAAN deze namespace nog gebruikt.

    ``leaving`` zijn de deployments die in DEZELFDE ronde worden verwijderd. Zonder dat
    onderscheid noemt elke zusterentry de namespace "in gebruik", ook een die net zo hard
    weggaat, en dan ruimt geen enkele beurt hem op (geval 5 van
    features/futures/verwijderpaden-in-kaart.md).
    """
    return any(
        other.get("name") != deployment_name
        and other.get("name") not in leaving
        and other.get("cluster") == cluster
        and other.get("namespace") == base_namespace
        for other in deployments
    )


def _application_files(project_dir: str) -> tuple[list[tuple[str, str, str]], list[str]]:
    """De Application-bestanden in een argo-projectmap.

    Returns:
        Per Application het pad, de ``metadata.name`` en het AppProject dat ze noemt, plus
        de bestanden die niet te lezen waren.
    """
    applications: list[tuple[str, str, str]] = []
    unreadable: list[str] = []
    for path in sorted(glob.glob(os.path.join(project_dir, "*.yaml"))):
        try:
            with open(path, encoding="utf-8") as handle:
                document = yaml.safe_load(handle)
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
            logger.warning(f"Could not read '{path}' in the ArgoCD folder of this project: {e}")
            unreadable.append(os.path.basename(path))
            continue
        if not isinstance(document, dict) or document.get("kind") != "Application":
            continue
        name = (document.get("metadata") or {}).get("name") or os.path.basename(path)
        applications.append((path, name, (document.get("spec") or {}).get("project") or ""))
    return applications, unreadable


def _applications_pointing_at_appproject(project_dir: str, appproject_name: str) -> list[str]:
    """Welke Applications in deze argo-map dit AppProject nog noemen.

    Een bestand dat niet te lezen is telt mee: onbekend is hier geen vrijbrief.
    """
    applications, unreadable = _application_files(project_dir)
    return [name for _, name, project in applications if project == appproject_name] + unreadable


def _remove_appproject_file(
    project_dir: str, project_name: str, base_namespace: str, namespace_used_by_others: bool
) -> bool:
    """Of het AppProject uit de map is; alleen als niets naar wijst (finalizers stallen; geval 1)."""
    appproject_name = generate_argocd_appproject_prefix(project_name, base_namespace)
    if namespace_used_by_others:
        logger.info(f"Keeping AppProject file '{appproject_name}': another deployment still uses this namespace")
        return False
    pointing = _applications_pointing_at_appproject(project_dir, appproject_name)
    if pointing:
        logger.info(f"Keeping AppProject file '{appproject_name}': still named by {', '.join(pointing)}")
        return False

    appproject_filename = get_output_filename_from_template("argocd-appproject.yaml.jinja", appproject_name)
    appproject_file_path = os.path.join(project_dir, appproject_filename)
    if os.path.exists(appproject_file_path):
        os.remove(appproject_file_path)
        logger.info(f"Deleted AppProject file: {appproject_filename}")
    return True


@dataclass(frozen=True)
class RemainingState:
    """Wat een hermeting van de lagen aan het eind van een verwijdering nog aantrof."""

    #: Lagen die er nog staan, als leesbare beschrijving per stuk.
    present: list[str]
    #: Lagen waarvan de meting zelf faalde, dus onbekend zijn.
    unreadable: list[str]

    @property
    def decides(self) -> bool:
        """Of deze meting het antwoord bepaalt: iets aangetroffen, of niets met alle lagen gemeten?"""
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
    """Manager for project and deployment deletion operations."""

    def __init__(self, project_manager: ProjectManager) -> None:
        """
        Initialize the DeleteProjectManager with reference to ProjectManager.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
        """
        self.project_manager = project_manager

    # Voortgangsstappen als in project_manager._begin_step.

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
        """De fouten die sinds ``errors_before`` bijkwamen, als sluitreden van een stap."""
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
        """De typen voor een inventaris, als ``(resource_types, own_types)``.

        De Application noemt haar eigen typen in ``status.resources``, en dat is het cluster
        zelf: die mogen de vraag smaller maken, en dat is wat geval 4 (Capsule) haalt. De
        terugval komt uit de manifest-repo van de tenant en mag dat NIET: een repo die een
        kind niet meer noemt zou de veegactie anders schoon laten melden terwijl de resource
        blijft staan. Ze zegt alleen welke ``Forbidden`` geen gat in de meting is.
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
        """De map in de deployments-repo waaronder de ``{cluster}/``-boom staat, of ``None``."""
        if not repository_name:
            return None
        repo_config = next(
            (r for r in project_data.get("repositories", []) or [] if r.get("name") == repository_name), None
        )
        if not repo_config:
            return None
        # Alleen om te LEZEN, en over alle projecten, dus niet via de werkmap van dit
        # project: een wegwerpmap met de hele ``{cluster}/``-boom erin.
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
        """Meet opnieuw: Application, AppProject, namespace nog aanwezig?
        Foutenlijst is logboek, niet toestand: de API is waarheid (geval 4 en 5)."""
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
        """
        Clean up orphaned ArgoCD Applications and AppProjects for a project.

        This method checks for ArgoCD resources that match the project name pattern
        but are not tracked in the project YAML file. This handles cases where:
        - Deployments were removed from YAML but ArgoCD resources remain
        - Previous deployment operations failed partway through
        - Manual edits to the project file removed deployments

        Args:
            project_name: Name of the project
            deletion_results: Dictionary to append operation results to
        """
        logger.info(f"Checking for orphaned ArgoCD resources for project '{project_name}'")

        try:
            argo_connector = create_argo_connector()
            kubectl = self.project_manager._kubectl_connector
            argo_namespace = get_argo_namespace(settings.CLUSTER_MANAGER)

            # List all applications and find ones matching this project
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

            # Also check for orphaned AppProjects
            # AppProjects typically follow pattern: {project_name}-{deployment_name} or {project_name}-infrastructure
            # (matched by `project_name in line` below).
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
        """Haal Applications uit git en wacht tot ze weg zijn; AppProject kan pas weg als map leeg is (geval 1)."""
        gitops_workspace = await self.project_manager.get_argo_workspace()
        working_dir = await gitops_workspace.get_working_dir()
        project_dir = os.path.join(working_dir, cluster, project_name)
        removed: list[str] = []
        unreadable: list[str] = []

        async def _verwijder_applications() -> bool:
            if not os.path.isdir(project_dir):
                return False
            applications, unreadable_files = _application_files(project_dir)
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
        await gitops_workspace.mutate_and_push(commit_message, _verwijder_applications)
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

        # Een bestand dat we niet konden lezen hebben we ook niet kunnen afwachten, en de
        # map gaat hierna in zijn geheel weg: Application-bestand en AppProject samen. Dus
        # telt het mee als een Application die blijft staan (geval 1).
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
            expect_folder: True wanneer het project deployments op DIT cluster had. Dan
                HOORT de map er te zijn en is een ontbrekende map een fout, geen
                schouderophalen. Zie hieronder waarom dat verschil telt.

        Returns:
            True wanneer de map deze ronde daadwerkelijk uit git is. Alleen dan wacht de
            hermeting op het verdwijnen van het AppProject uit het cluster.

        Een ontbrekende map is namelijk twee heel verschillende dingen. Bij een project
        zonder deployments op dit cluster is er nooit een map geweest en klopt "niet
        gevonden". Bij een project MET deployments hoort hij er te zijn, en betekent
        "niet gevonden" dat we hem niet konden vinden - niet dat hij er niet is.

        Dat onderscheid ontbrak, en dat is precies hoe er vijf verweesde ArgoCD-mappen
        ontstonden: de verwijdering noteerde ``not_found``, ging door, en gooide het
        projectbestand weg. Daarna stond de map in de repo zonder project ernaast, maakte
        de root-application de Application telkens opnieuw aan, faalde die op
        ``app path does not exist`` en probeerde het met ``retry limit -1`` elke 30
        seconden opnieuw. Niets ruimde dat ooit op.
        """
        project_argocd_folder_rel = os.path.join(cluster, project_name)
        logger.info(f"Deleting project ArgoCD folder: {project_argocd_folder_rel}")

        try:
            gitops_workspace = await self.project_manager.get_argo_workspace()
            working_dir = await gitops_workspace.get_working_dir()
            project_argocd_folder = os.path.join(working_dir, project_argocd_folder_rel)

            # De BESLISSING hoort binnen de lock, op de stand die reset_to_remote daar
            # ophaalt: een gecachte kloon kan willekeurig oud zijn, en een andere taak kan
            # deze projectmap intussen herschreven hebben.
            async def _verwijder_projectmap() -> bool:
                if not os.path.exists(project_argocd_folder):
                    return False
                await asyncio.to_thread(shutil.rmtree, project_argocd_folder)
                return True

            commit_message = f"Delete ArgoCD resources for project '{project_name}'"
            verwijderd = await gitops_workspace.mutate_and_push(commit_message, _verwijder_projectmap)

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
                # Hij hoorde er te zijn. Doorgaan zou het projectbestand weggooien en de map
                # laten staan, en dat is precies de wees die niemand daarna nog terugvindt.
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
        """
        Clean up project-level Keycloak resources for a cluster.

        Called when the last deployment in a cluster is deleted.

        Steps:
        1. Delete project realm
        2. Delete project admin user from master realm
        3. Delete platform client from RIG Platform realm
        4. Remove keycloak config entry from project.yaml

        Args:
            project_name: Name of the project
            cluster: Name of the cluster
            kc_config: Keycloak config entry with host/realm/username/password
            deletion_results: Results dictionary to append deletion operations to
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

            # 1. Delete project realm
            try:
                await keycloak.delete_realm(realm_name)
                logger.info(f"Deleted project realm {realm_name}")
                deletion_results["operations"].append(
                    {"type": "keycloak_realm_deletion", "target": realm_name, "status": "success"}
                )
            except Exception as e:
                logger.exception(f"Failed to delete realm {realm_name}")
                deletion_results["errors"].append(f"Realm deletion: {e}")

            # 2. Delete project admin from master realm
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

            # 3. Delete platform client from RIG Platform realm
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

            # 4. Remove keycloak config entry from project.yaml
            try:
                project_data = await self.project_manager.get_contents()
                # RC-5 B: keycloak connections live under the keycloak service config.
                view = Project(project_data)
                keycloak_list = view.get("services/keycloak/config/realms") or []

                # Remove entry matching this realm
                updated_list = [kc for kc in keycloak_list if kc.get("realm") != realm_name]

                if updated_list != keycloak_list:
                    view.set("services/keycloak/config/realms", updated_list)
                    # Central save: writes and commits as one locked operation. This used
                    # to be save_project_data(), which wrote the file into the shared warm
                    # working copy and never committed it -- leaving it to be swept up by
                    # whichever unrelated operation ran `git add -A` next, or discarded by
                    # a concurrent reconcile before that happened.
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
        """
        Clean up project-level infrastructure resources.

        Called during project deletion to remove infrastructure that is shared across
        all deployments (e.g., namespace-specific PostgreSQL clusters, Redis instances).

        Steps:
        1. Delete infrastructure ArgoCD folder from GitOps repo
        2. Refresh user-applications
        3. Wait for infrastructure ArgoCD Application deletion
        4. Delete infrastructure namespace
        5. Delete infrastructure manifests from deployment git repo

        Args:
            project_name: Name of the project
            cluster: Cluster name (e.g., "local", "odcn-production")
            project_data: Project configuration dictionary
            deletion_results: Results dictionary to append operations/errors to
            force: If True, continues on errors and removes finalizers if needed
        """
        from opi.core.cluster_config import get_infrastructure_namespace
        from opi.services.services import ServiceType
        from opi.utils.naming import generate_infrastructure_manifest_path

        # Services that require dedicated infrastructure namespace
        NAMESPACE_SERVICES = {
            ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value,
            ServiceType.NAMESPACE_REDIS.value,
        }

        # Check if project uses any namespace-specific service
        project_services = project_data.get("services", [])
        # service_entry_name resolves all three entry formats. Matching on the raw dict
        # keys only saw the legacy single-key form, so a namespace service carrying
        # config went undetected here and its infrastructure was left behind on delete.
        # postgresql-database with scope: project also owns an infrastructure namespace
        # (RC-17), so include it or its dedicated cluster leaks on delete.
        uses_namespace_infrastructure = any(
            service_entry_name(entry) in NAMESPACE_SERVICES for entry in project_services
        ) or project_uses_dedicated_postgres(project_data)

        if not uses_namespace_infrastructure:
            logger.debug(
                f"Project '{project_name}' does not use namespace-specific services, skipping infrastructure deletion"
            )
            return

        logger.info(f"Cleaning up project-level infrastructure for project '{project_name}'")

        try:
            # 1. Delete infrastructure ArgoCD folder from GitOps
            infra_app_name = generate_infrastructure_application_name(project_name)
            infra_argocd_folder_rel = generate_infrastructure_argocd_folder_path(cluster, project_name)
            logger.info(f"Deleting infrastructure ArgoCD folder: {infra_argocd_folder_rel}")

            gitops_workspace = await self.project_manager.get_argo_workspace()
            working_dir = await gitops_workspace.get_working_dir()
            infra_argocd_folder = os.path.join(working_dir, infra_argocd_folder_rel)

            async def _verwijder_inframap() -> bool:
                if not os.path.exists(infra_argocd_folder):
                    return False
                await asyncio.to_thread(shutil.rmtree, infra_argocd_folder)
                return True

            commit_message = f"Delete infrastructure ArgoCD application for project '{project_name}'"
            verwijderd = await gitops_workspace.mutate_and_push(commit_message, _verwijder_inframap)

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

            # 2. Refresh user-applications
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

            # 3. Wait for infrastructure Application deletion
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

            # 4. Delete infrastructure namespace (only if app was confirmed deleted)
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

            # 5. Delete infrastructure manifests folder from deployment git repo
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
        """Verwijder ``<cluster>/<project>/_project`` uit de deployments-repo.

        Het projectniveau hangt aan het project, dus de per-deployment opruiming raakt het
        nooit. Welke repository de map draagt beslist ``project_level_deployment``,
        dezelfde functie als de schrijver en de ArgoCD-applicatie gebruiken.
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
        """Verwijder namespaces nu geen deployment er meer naar wijst (geval 5 en 6.1-1)."""
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

            # De Kubernetes-API is de waarheid: een delete die werd aangenomen zegt niet dat
            # de namespace weg is, want zijn finalizers kunnen nog lopen.
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
        """Meet de lagen opnieuw, en haal het projectbestand pas weg als er niets meer staat.

        De fouten van de rit blijven als uitleg in het resultaat staan; ze beslissen niets
        meer. Kon de meting niet worden gedaan, dan beslist de rit alsnog: onbekend is geen
        bewijs, in geen van beide richtingen.
        """
        stap = self._begin_step("Controleren dat alles verwijderd is")
        fouten_voor = len(deletion_results["errors"])
        appprojects = [
            generate_argocd_appproject_prefix(project_name, dep.get("namespace") or project_name) for dep in deployments
        ]
        if appprojects_left_git:
            # ArgoCD prunt het AppProject asynchroon nadat zijn manifest uit git is; zonder
            # deze wacht meet de hermeting de tussenstand en faalt een delete die seconden
            # later schoon was.
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
        self._end_step(stap, self._new_errors(deletion_results, fouten_voor))

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
        stap = self._begin_step("Projectbestand verwijderen")
        fouten_voor = len(deletion_results["errors"])
        delete_result = await self._delete_project_file(project_name, commit_message)
        deletion_results["operations"].extend(delete_result["operations"])
        deletion_results["errors"].extend(delete_result["errors"])
        if not delete_result["success"]:
            deletion_results["success"] = False
        self._end_step(stap, self._new_errors(deletion_results, fouten_voor))

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
            force: If True, continues on errors and cleans up stuck resources.
                   Use when a previous deletion failed partially. Het haalt het
                   projectbestand NIET weg zolang er nog iets staat: dat is de enige
                   onomkeerbare stap, en het bestand is de invoer van elke nieuwe poging.

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

        # Look up actual filename from project service (filename may differ from project name)
        project = get_project_store().get(project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in project service")
        self.project_manager._project_file_relative_path = f"projects/{project.filename}"

        try:
            # Step 1: Read project configuration
            project_data = await self.project_manager.get_contents()

            # Step 2: Get deployments separated by cluster using helper methods
            current_cluster = settings.CLUSTER_MANAGER
            current_cluster_deployments = await self.project_manager.get_deployments(cluster_filter=True)
            all_deployments = await self.project_manager.get_deployments(cluster_filter=False)
            other_cluster_deployments = [d for d in all_deployments if d.get("cluster") != current_cluster]

            logger.info(
                f"Project {project_name} has {len(current_cluster_deployments)} deployments on current cluster '{current_cluster}' "
                f"and {len(other_cluster_deployments)} deployments on other clusters"
            )

            # Step 3: Check if there are deployments on other clusters
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

            # Step 4: Delete all deployments on the current cluster
            #
            # Elke deployment van deze ronde kent de namen van de andere, zodat de grendel
            # op de namespace een zuster die net zo hard weggaat niet voor een blijver
            # aanziet (geval 5).
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
                        # Ook met force: een deployment die faalde maakt de
                        # projectverwijdering niet geslaagd. force betekent "stop niet bij
                        # de eerste fout", niet "noem een halve verwijdering klaar".
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

            # Step 4.5: Clean up project-level infrastructure (namespace-specific PostgreSQL/Redis)
            if deletion_results["success"] or force:
                stap = self._begin_step("Projectinfrastructuur opruimen")
                fouten_voor = len(deletion_results["errors"])
                await self._cleanup_project_infrastructure(
                    project_name, current_cluster, project_data, deletion_results, force
                )
                self._end_step(stap, self._new_errors(deletion_results, fouten_voor))

            # Step 4.6: Clean up project Keycloak realm(s)
            if deletion_results["success"] or force:
                stap = self._begin_step("Keycloak-realm verwijderen")
                fouten_voor = len(deletion_results["errors"])
                kc_config = await self.project_manager._get_project_keycloak_config_for_cluster(current_cluster)
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
                    # cluster, so they can be removed without the file telling us. Skipping
                    # here used to leave both behind silently, and an orphaned admin account
                    # that still carries an OTP credential is not something to leave lying
                    # around because a config block went missing.
                    await self._cleanup_project_keycloak_realm(
                        project_name=project_name,
                        cluster=current_cluster,
                        kc_config={
                            "realm": generate_project_realm_name(project_name, current_cluster),
                            "username": generate_project_admin_username(project_name, current_cluster),
                            "host": self.project_manager._get_keycloak_url_for_cluster(current_cluster),
                        },
                        deletion_results=deletion_results,
                        only_if_present=True,
                    )
                self._end_step(stap, self._new_errors(deletion_results, fouten_voor))

            # Step 4.65 en 4.7 samen zijn één zichtbare stap. De grendels blijven per deel
            # opnieuw meten, dus de stap begint zodra het eerste deel draait en kan daarna
            # niet meer aankloppen zonder dat het werk liep.
            stap_argo: str | None = None
            fouten_voor_argo = len(deletion_results["errors"])
            appprojects_left_git = False

            # Step 4.65: Delete the project level of the deployments repo
            if deletion_results["success"] or force:
                stap_argo = self._begin_step("ArgoCD-resources van het project verwijderen")
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
                    self._end_step(stap_argo, str(e))
                    raise
                if applications_released:
                    appprojects_left_git = await self._delete_project_argocd_folder(
                        project_name,
                        current_cluster,
                        deletion_results,
                        expect_folder=len(current_cluster_deployments) > 0,
                    )
            self._end_step(stap_argo, self._new_errors(deletion_results, fouten_voor_argo))

            # Step 4.8: De namespaces van dit project, nu er geen deployment meer naar wijst
            if deletion_results["success"] or force:
                stap = self._begin_step("Namespaces verwijderen")
                fouten_voor = len(deletion_results["errors"])
                await self._delete_project_namespaces(
                    project_name, current_cluster, current_cluster_deployments, deletion_results
                )
                self._end_step(stap, self._new_errors(deletion_results, fouten_voor))

            # Step 4.9 en 5: de hermeting bepaalt de uitkomst, en daarmee of het
            # projectbestand mag gaan. Het projectbestand is de invoer van elke latere ronde
            # en van de orphan-sweep, dus zolang er iets staat moet het blijven staan: anders
            # kan niemand de verwijdering nog afmaken of zelfs melden (geval 2c en 5). force
            # overrulet dat niet meer, want dat is hoe rig-rl437-56m zijn namespace hield
            # zonder projectbestand ernaast.
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

                # Clean up any orphaned ArgoCD resources that may exist despite no deployments in YAML
                await self._cleanup_orphaned_argocd_resources(project_name, deletion_results)

                commit_message = f"Delete project '{project_name}' - no deployments remaining"
                stap = self._begin_step("Projectbestand verwijderen")
                fouten_voor = len(deletion_results["errors"])
                delete_result = await self._delete_project_file(project_name, commit_message)

                deletion_results["operations"].extend(delete_result["operations"])
                deletion_results["errors"].extend(delete_result["errors"])
                if not delete_result["success"]:
                    deletion_results["success"] = False
                self._end_step(stap, self._new_errors(deletion_results, fouten_voor))

            # Clean up subdomain registrations for this project
            stap = self._begin_step("Subdomeinen opruimen")
            fouten_voor = len(deletion_results["errors"])
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
            self._end_step(stap, self._new_errors(deletion_results, fouten_voor))

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
        """
        Delete the project file from the git repository.

        Args:
            project_name: Name of the project to delete
            commit_message: Commit message for the deletion

        Returns:
            Dictionary containing operation result
        """
        result = {"success": True, "operations": [], "errors": []}

        try:
            # Look up actual filename from project service (filename may differ from project name)
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
        """
        Delete all resources associated with a specific deployment.

        This function orchestrates the deletion of:
        1. Service resources (Keycloak clients, database resources, MinIO resources)
        2. Application manifests from git repositories
        3. ArgoCD applications
        4. Kubernetes namespace (last, after ArgoCD cleanup)

        Args:
            project_name: Name of the project
            deployment_name: Name of the deployment to delete
            force: If True, continues on errors and cleans up stuck resources.
                   In force mode:
                   - Deletes the resources of an Application whose deletion times out, and
                     removes its finalizer only when that sweep got everything
                   - Skips database cleanup if secrets are inaccessible
                   - Only deletes namespace after ArgoCD app is confirmed deleted
            leaving: De namen van de deployments die in DEZELFDE ronde worden verwijderd.
                   Een zuster uit die verzameling houdt de namespace niet tegen, want ze
                   gaat net zo hard weg (geval 5).

        Returns:
            Dictionary containing deletion results and status

        Raises:
            HTTPException: If critical operations fail (unless force=True)
        """

        deletion_results: dict[str, Any] = {
            "project": project_name,
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
            "service_results": {},
            "force_mode": force,
            # "It is gone" and "it was never there" are both success, and a script
            # cannot act on the difference unless the answer states it (RC-66).
            "already_absent": False,
        }

        if force:
            logger.info(f"Force mode enabled for deployment deletion: {project_name}/{deployment_name}")

        # Look up actual filename from project service (filename may differ from project name)
        project = get_project_store().get(project_name)
        if not project:
            raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found in project service")
        self.project_manager._project_file_relative_path = f"projects/{project.filename}"

        try:
            # Step 1: Read project configuration
            git_connector = await self.project_manager.get_git_connector_for_project_files()
            await git_connector.ensure_repo_cloned()

            project_data = await self.project_manager.get_contents()

            # Find the specific deployment using helper method
            deployment = await self.project_manager.get_deployment_by_name(deployment_name)

            if not deployment:
                raise HTTPException(
                    status_code=404, detail=f"Deployment '{deployment_name}' not found in project '{project_name}'"
                )

            logger.info(f"Starting deployment deletion for {project_name}/{deployment_name}")

            repository_name = deployment.get("repository")
            cluster = deployment.get("cluster")

            stap_argo = self._begin_step("ArgoCD-toepassing verwijderen", subject=deployment_name)
            fouten_voor_argo = len(deletion_results["errors"])

            # Step 2: Delete ArgoCD application file from GitOps
            logger.info(f"Deleting ArgoCD application file for {project_name}/{deployment_name}")

            gitops_workspace = await self.project_manager.get_argo_workspace()
            argocd_app_file_path = generate_gitops_argocd_application_path(cluster, project_name, deployment_name)
            logger.info(f"Attempting to delete ArgoCD application file: {argocd_app_file_path}")

            working_dir = await gitops_workspace.get_working_dir()
            project_dir = os.path.join(working_dir, cluster, project_name)
            file_full_path = os.path.join(working_dir, argocd_app_file_path)

            # De AppProject en de repository-secrets gaan mee in DEZE mutatie. Ze stonden na
            # de commit, dus hun verwijdering bleef ongecommit in de werkboom staan, en de
            # kustomization die er net voor herbouwd werd noemde ze nog.
            current_base_namespace = deployment.get("namespace")
            namespace_used_by_others = _namespace_used_by_others(
                project_data.get("deployments", []), deployment_name, cluster, current_base_namespace, leaving
            )
            current_repo = deployment.get("repository")
            repo_used_by_others = current_repo is not None and any(
                other_dep.get("name") != deployment_name and other_dep.get("repository") == current_repo
                for other_dep in project_data.get("deployments", [])
            )

            appproject_left_git = False

            async def _verwijder_argocd_bestanden() -> None:
                nonlocal appproject_left_git
                if os.path.exists(file_full_path):
                    os.remove(file_full_path)
                    deletion_results["operations"].append(
                        {
                            "type": "argocd_application_file_deletion",
                            "target": argocd_app_file_path,
                            "cluster": cluster,
                            "deployment": deployment_name,
                            "status": "success",
                        }
                    )
                    logger.info(f"Successfully deleted ArgoCD application file: {argocd_app_file_path}")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "argocd_application_file_deletion",
                            "target": argocd_app_file_path,
                            "cluster": cluster,
                            "deployment": deployment_name,
                            "status": "not_found",
                        }
                    )

                appproject_left_git = _remove_appproject_file(
                    project_dir, project_name, current_base_namespace, namespace_used_by_others
                )

                if current_repo and not repo_used_by_others:
                    unique_repo_name = f"{project_name}-{current_repo}"
                    for template in ["argo-repository-https.yaml.jinja", "argo-repository.yaml.jinja"]:
                        filename = get_output_filename_from_template(template, unique_repo_name)
                        file_path = os.path.join(project_dir, filename)
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            logger.info(f"Deleted repository file: {filename}")

                # Rebuild kustomization file
                logger.info(f"Rebuilding kustomization.yaml for project {project_name} in cluster {cluster}")
                kustomization_success = self.project_manager._manifest_generator.create_kustomization_files(
                    output_dir=project_dir,
                    namespace=get_argo_namespace(cluster),
                )

                if kustomization_success:
                    deletion_results["operations"].append(
                        {
                            "type": "kustomization_rebuild",
                            "target": project_dir,
                            "cluster": cluster,
                            "status": "success",
                        }
                    )
                    logger.info(f"Successfully rebuilt kustomization.yaml for project {project_name}")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "kustomization_rebuild",
                            "target": project_dir,
                            "cluster": cluster,
                            "status": "failed",
                        }
                    )
                    logger.error(f"Failed to rebuild kustomization.yaml for project {project_name}")

            # Let git exceptions bubble up for worker retry.
            commit_message = f"Delete ArgoCD application for deployment '{deployment_name}' from project '{project_name}' and rebuild kustomization"
            try:
                await gitops_workspace.mutate_and_push(commit_message, _verwijder_argocd_bestanden)
            except Exception as e:
                self._end_step(stap_argo, str(e))
                raise
            deletion_results["operations"].append(
                {"type": "gitops_commit", "status": "success", "message": commit_message}
            )

            # Step 3: Refresh user-applications
            try:
                argo_connector = create_argo_connector()
                refresh_success = await argo_connector.refresh_application("user-applications")
                if refresh_success:
                    deletion_results["operations"].append(
                        {"type": "argocd_refresh", "target": "user-applications", "status": "success"}
                    )
                    logger.info("Successfully refreshed user-applications after GitOps deletion")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "argocd_refresh",
                            "target": "user-applications",
                            "status": "failed",
                            "error": "Failed to refresh user-applications",
                        }
                    )
                    logger.warning("Failed to refresh user-applications - continuing anyway")
            except Exception as refresh_error:
                deletion_results["operations"].append(
                    {
                        "type": "argocd_refresh",
                        "target": "user-applications",
                        "status": "error",
                        "error": str(refresh_error),
                    }
                )
                logger.warning(f"Error refreshing user-applications: {refresh_error} - continuing anyway")

            # Step 4: Wait for ArgoCD application deletion
            argocd_app_deleted = False  # Track if app was confirmed deleted
            app_name = generate_argocd_application_name(project_name, deployment_name)

            try:
                app_exists = await argo_connector.application_exists(app_name)
                if app_exists:
                    logger.info(f"Waiting for ArgoCD application {app_name} to be deleted via GitOps")
                    await self._terminate_application_operation(app_name, deletion_results)
                    deletion_complete = await argo_connector.wait_for_application_deletion(
                        app_name,
                        max_retries=40,
                        retry_delay=5,
                        kubectl_connector=self.project_manager._kubectl_connector,
                        progress_window_seconds=deletion_progress_window(),
                    )

                    if deletion_complete:
                        deletion_results["operations"].append(
                            {
                                "type": "argocd_app_gitops_deletion",
                                "target": app_name,
                                "cluster": cluster,
                                "deployment": deployment_name,
                                "status": "success",
                            }
                        )
                        logger.info(f"ArgoCD application {app_name} successfully deleted via GitOps")
                        argocd_app_deleted = True
                    else:
                        # Timeout - in force mode, try to remove finalizers
                        finalizer_removed = False
                        if force:
                            logger.warning(
                                f"ArgoCD application {app_name} deletion timed out - "
                                "force mode: deleting its resources, then removing finalizers"
                            )
                            outcome = await self._force_delete_or_await_cascade(
                                app_name,
                                deletion_results,
                                argo_connector,
                                await self._deployments_repo_dir(project_data, repository_name),
                            )
                            argocd_app_deleted = outcome.application_gone
                            finalizer_removed = outcome.finalizer_removed

                        deletion_results["operations"].append(
                            {
                                "type": "argocd_app_gitops_deletion",
                                "target": app_name,
                                "cluster": cluster,
                                "deployment": deployment_name,
                                "status": "timeout",
                                "error": "Application deletion via GitOps timed out",
                                "finalizer_removed": finalizer_removed,
                            }
                        )
                        if not force:
                            logger.warning(f"ArgoCD application {app_name} deletion timed out - continuing anyway")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "argocd_app_gitops_deletion",
                            "target": app_name,
                            "cluster": cluster,
                            "deployment": deployment_name,
                            "status": "not_found",
                        }
                    )
                    logger.info(f"ArgoCD application {app_name} was not found (already deleted or never existed)")
                    argocd_app_deleted = True  # Consider it deleted if not found

            except PermissionError:
                # Permission denied means the AppProject was deleted before the Application.
                # This indicates the Application is deleted or will be garbage collected.
                deletion_results["operations"].append(
                    {
                        "type": "argocd_app_gitops_deletion",
                        "target": app_name,
                        "cluster": cluster,
                        "deployment": deployment_name,
                        "status": "success",
                        "note": "Permission denied (AppProject deleted), treating as deleted",
                    }
                )
                logger.info(
                    f"ArgoCD application {app_name} - permission denied (AppProject deleted), treating as deleted"
                )
                argocd_app_deleted = True

            except Exception as e:
                deletion_results["operations"].append(
                    {
                        "type": "argocd_app_gitops_deletion",
                        "target": app_name,
                        "cluster": cluster,
                        "deployment": deployment_name,
                        "status": "error",
                        "error": str(e),
                    }
                )
                logger.exception("Error monitoring ArgoCD application deletion")
                if force:
                    argocd_app_deleted = True  # In force mode, continue anyway

            self._end_step(stap_argo, self._new_errors(deletion_results, fouten_voor_argo))

            # Step 5: Delete Kubernetes namespace (only if ArgoCD app was confirmed deleted)
            namespace_should_be_gone = False
            stap_ns: str | None = None
            fouten_voor_ns = len(deletion_results["errors"])
            try:
                base_namespace = deployment.get("namespace", project_name)
                namespace = get_prefixed_namespace(cluster, base_namespace)

                namespace_used_by_others = _namespace_used_by_others(
                    project_data.get("deployments", []), deployment_name, cluster, base_namespace, leaving
                )

                # Only delete namespace if ArgoCD app was confirmed deleted
                # This prevents orphaning ArgoCD apps that can't clean up their resources
                if not argocd_app_deleted and not force:
                    deletion_results["operations"].append(
                        {
                            "type": "namespace_deletion",
                            "target": namespace,
                            "cluster": cluster,
                            "deployment": deployment_name,
                            "status": "skipped",
                            "reason": "ArgoCD application not confirmed deleted - skipping to prevent orphaned app",
                        }
                    )
                    deletion_results["errors"].append(
                        f"Namespace '{namespace}' not deleted: ArgoCD application '{app_name}' was not confirmed deleted. "
                        "Use force=true to override."
                    )
                    deletion_results["success"] = False
                    logger.warning(
                        f"Skipping namespace deletion - ArgoCD app {app_name} not confirmed deleted. "
                        "This prevents orphaning the ArgoCD application."
                    )
                elif not namespace_used_by_others:
                    namespace_should_be_gone = True
                    if not argocd_app_deleted:
                        logger.warning(
                            f"Force mode: deleting namespace {namespace} even though ArgoCD app status is uncertain"
                        )
                    logger.info(f"Deleting Kubernetes namespace: {namespace}")
                    stap_ns = self._begin_step("Namespace verwijderen", subject=deployment_name)
                    namespace_deleted = await self.project_manager._kubectl_connector.delete_namespace(namespace)

                    if namespace_deleted:
                        deletion_results["operations"].append(
                            {
                                "type": "namespace_deletion",
                                "target": namespace,
                                "cluster": cluster,
                                "deployment": deployment_name,
                                "status": "success",
                            }
                        )
                        logger.info(f"Successfully deleted namespace: {namespace}")
                    else:
                        # delete_namespace uses --ignore-not-found, so a genuinely absent
                        # namespace returns True. A False here is therefore a real delete
                        # failure (e.g. an RBAC 403), NOT "already gone" -- surface it
                        # instead of silently reporting success and leaking the namespace.
                        deletion_results["operations"].append(
                            {
                                "type": "namespace_deletion",
                                "target": namespace,
                                "cluster": cluster,
                                "deployment": deployment_name,
                                "status": "error",
                                "error": "kubectl delete namespace failed (see logs); namespace left behind",
                            }
                        )
                        deletion_results["errors"].append(
                            f"Failed to delete namespace '{namespace}' (see logs for the kubectl error)."
                        )
                        deletion_results["success"] = False
                        logger.error(f"Failed to delete namespace {namespace}; it was left behind")
                    self._end_step(stap_ns, self._new_errors(deletion_results, fouten_voor_ns))
                    stap_ns = None
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "namespace_deletion",
                            "target": namespace,
                            "cluster": cluster,
                            "deployment": deployment_name,
                            "status": "skipped",
                            "reason": "Namespace still used by other deployments",
                        }
                    )
                    logger.info(f"Skipping namespace deletion - namespace {namespace} still used by other deployments")

            except Exception as e:
                self._end_step(stap_ns, str(e))
                deletion_results["operations"].append(
                    {
                        "type": "namespace_deletion",
                        "target": namespace,
                        "cluster": cluster,
                        "deployment": deployment_name,
                        "status": "error",
                        "error": str(e),
                    }
                )
                deletion_results["errors"].append(f"Error deleting namespace {namespace}: {e}")
                logger.exception(f"Error deleting namespace {namespace}")

            # Step 6: Delete service resources (calls service managers)
            stap_svc = self._begin_step("Services opruimen", subject=deployment_name)
            fouten_voor_svc = len(deletion_results["errors"])
            logger.info(f"Deleting service resources for {project_name}/{deployment_name}")

            # Delete Keycloak resources (always immediate - ephemeral)
            keycloak_results = await self.project_manager._keycloak_manager.delete_resources_for_deployment(
                project_data, deployment
            )
            deletion_results["service_results"]["keycloak"] = keycloak_results
            deletion_results["operations"].extend(keycloak_results["operations"])
            if keycloak_results["errors"]:
                deletion_results["errors"].extend(keycloak_results["errors"])

            # Determine if data resources should be marked for deferred deletion
            # based on the deployment's data-retention-period setting
            retention_period = deployment.get("data-retention-period")
            retention_hours = 0
            try:
                retention_hours = parse_retention_period_hours(retention_period)
            except ValueError as e:
                logger.warning(f"Invalid data-retention-period for {deployment_name}: {e}, using immediate deletion")

            marked_for_deletion_service: MarkedForDeletionService | None = None
            if retention_hours > 0:
                try:
                    from opi.core.database_pools import get_database_pool
                    from opi.services.marked_for_deletion_service import MarkedForDeletionService

                    get_database_pool("main")  # guard: raises if the DB is unavailable -> immediate delete
                    marked_for_deletion_service = MarkedForDeletionService()
                    logger.info(
                        f"Using deferred deletion for {project_name}/{deployment_name} "
                        f"(data-retention-period: {retention_period}, {retention_hours}h)"
                    )
                except KeyError, ValueError:
                    logger.warning(
                        "Database pool not available - cannot use deferred deletion, falling back to immediate deletion"
                    )

            # Delete/mark database resources
            try:
                db_manager = await self.project_manager._ensure_database_manager()
                if marked_for_deletion_service is not None:
                    database_results = await db_manager.handle_service_removal(
                        project_name=project_name,
                        deployment_name=deployment_name,
                        deployment_data=deployment,
                        project_data=project_data,
                        marked_for_deletion_service=marked_for_deletion_service,
                    )
                else:
                    database_results = await db_manager.delete_resources_for_deployment(project_data, deployment)
                deletion_results["service_results"]["database"] = database_results
                deletion_results["operations"].extend(database_results.get("operations", []))
                if database_results.get("errors"):
                    deletion_results["errors"].extend(database_results["errors"])
            except Exception as db_error:
                if force:
                    logger.warning(f"Force mode: could not delete database resources ({db_error}), skipping")
                    deletion_results["service_results"]["database"] = {
                        "operations": [],
                        "errors": [str(db_error)],
                        "skipped": True,
                        "force_mode": True,
                    }
                    deletion_results["operations"].append(
                        {
                            "type": "database_resource_deletion",
                            "status": "skipped",
                            "reason": str(db_error),
                            "force_mode": True,
                        }
                    )
                else:
                    self._end_step(stap_svc, str(db_error))
                    raise

            # Delete/mark MinIO resources
            if marked_for_deletion_service is not None:
                minio_results = await self.project_manager._minio_manager.handle_service_removal(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    deployment_data=deployment,
                    project_data=project_data,
                    marked_for_deletion_service=marked_for_deletion_service,
                )
            else:
                minio_results = await self.project_manager._minio_manager.delete_resources_for_deployment(
                    project_data, deployment
                )
            deletion_results["service_results"]["minio"] = minio_results
            deletion_results["operations"].extend(minio_results.get("operations", []))
            if minio_results.get("errors"):
                deletion_results["errors"].extend(minio_results["errors"])

            # Dezelfde tak voor seaweedfs-storage (RC-331): deze plek roept de manager
            # rechtstreeks aan en gaat niet via cleanup_manager_key, dus een tweede dienst
            # moet er met de hand bij. De guard houdt het antwoord van een deployment
            # zonder deze dienst gelijk, zoals de tweede plek met has_seaweedfs doet.
            if self.project_manager._project_file_handler.deployment_uses_service(
                project_data, deployment_name, [ServiceType.SEAWEEDFS_STORAGE.value]
            ):
                if marked_for_deletion_service is not None:
                    seaweedfs_results = await self.project_manager._seaweedfs_manager.handle_service_removal(
                        project_name=project_name,
                        deployment_name=deployment_name,
                        deployment_data=deployment,
                        project_data=project_data,
                        marked_for_deletion_service=marked_for_deletion_service,
                    )
                else:
                    seaweedfs_results = await self.project_manager._seaweedfs_manager.delete_resources_for_deployment(
                        project_data, deployment
                    )
                deletion_results["service_results"]["seaweedfs"] = seaweedfs_results
                deletion_results["operations"].extend(seaweedfs_results.get("operations", []))
                if seaweedfs_results.get("errors"):
                    deletion_results["errors"].extend(seaweedfs_results["errors"])

            # Het mailaccount van deze deployment (RC-401). Het account is van de
            # deployment, dus het gaat mee met de deployment; zonder deze stap bleef het op
            # de relay staan en nam alleen het weghalen van de DIENST het mee.
            if self.project_manager._project_file_handler.deployment_uses_service(
                project_data, deployment_name, [ServiceType.SEND_EMAIL.value]
            ):
                mail_results = await self.project_manager._mail_manager.delete_resources_for_deployment(
                    project_data, deployment
                )
                deletion_results["service_results"]["send_email"] = mail_results
                deletion_results["operations"].extend(mail_results.get("operations", []))
                if mail_results.get("errors"):
                    deletion_results["errors"].extend(mail_results["errors"])

            self._end_step(stap_svc, self._new_errors(deletion_results, fouten_voor_svc))

            # Step 7: Delete deployment folders from git repositories
            # Uitgestelde opruiming (RC-254) handelt intentioneel staan gelaten maps af.
            # Alleen git-fouten zetten dit op False en blokken stap 8.
            manifest_folder_removed = True
            # Zowel de verwijdering van de map als zijn markering voor uitgestelde
            # opruiming horen bij deze stap; zonder repository is er geen van beide.
            fouten_voor_manifesten = len(deletion_results["errors"])
            stap_manifesten = (
                self._begin_step("Manifesten verwijderen", subject=deployment_name)
                if not argocd_app_deleted or (repository_name and cluster)
                else None
            )

            # IMPORTANT: Only delete manifests if ArgoCD app is confirmed deleted.
            # If the app still exists, its resources-finalizer needs the source path
            # to determine which K8s resources to clean up. Deleting the manifests
            # while the finalizer is still running causes a permanent deadlock.
            if not argocd_app_deleted:
                logger.warning(
                    f"Skipping deployment manifest deletion for {project_name}/{deployment_name} - "
                    f"ArgoCD application not confirmed deleted. Marking for deferred cleanup."
                )
                try:
                    from opi.services.marked_for_deletion_service import MarkedForDeletionService as MFDService

                    # MFDService is ORM-backed and takes no constructor arguments.
                    # Passing a pool raised TypeError, which the except below swallowed,
                    # so manifests were never actually marked for deferred cleanup.
                    deferred_service = MFDService()
                    resource_name = f"{cluster}/{project_name}/{deployment_name}"
                    await deferred_service.mark_resource(
                        resource_type="deployment_manifests",
                        resource_name=resource_name,
                        project_name=project_name,
                        deployment_name=deployment_name,
                        cluster=cluster,
                        metadata={
                            "repository_name": repository_name,
                            "argocd_app_name": app_name,
                        },
                    )
                    deletion_results["operations"].append(
                        {
                            "type": "deployment_folder_deletion",
                            "status": "deferred",
                            "reason": "ArgoCD application not confirmed deleted - marked for retry",
                        }
                    )
                except Exception as mark_err:
                    logger.warning(
                        f"Could not mark manifests for deferred deletion: {mark_err}. "
                        "Manifests will remain in git until manually cleaned up."
                    )
                    deletion_results["operations"].append(
                        {
                            "type": "deployment_folder_deletion",
                            "status": "skipped",
                            "reason": "ArgoCD app not deleted and could not mark for retry",
                        }
                    )
            elif repository_name and cluster:
                try:
                    repositories = project_data.get("repositories", [])
                    repo_config = None
                    for repo in repositories:
                        if repo.get("name") == repository_name:
                            repo_config = repo
                            break

                    if repo_config:
                        workspace = await self.project_manager.get_deployment_workspace(repository_name, repo_config)

                        repo_path = repo_config.get("path", "")
                        deployment_folder_path = generate_deployment_manifest_path(
                            cluster, project_name, deployment_name, repo_path
                        )
                        logger.info(f"Attempting to delete deployment folder: {deployment_folder_path}")

                        commit_message = f"Delete deployment '{deployment_name}' from project '{project_name}'"
                        folder_exists = await workspace.remove_and_push(deployment_folder_path, commit_message)

                        if folder_exists:
                            deletion_results["operations"].append(
                                {
                                    "type": "deployment_folder_deletion",
                                    "target": deployment_folder_path,
                                    "repository": repository_name,
                                    "cluster": cluster,
                                    "status": "success",
                                }
                            )
                            logger.info(f"Successfully deleted deployment folder: {deployment_folder_path}")
                            deletion_results["operations"].append(
                                {
                                    "type": "manifest_repo_commit",
                                    "repository": repository_name,
                                    "status": "success",
                                    "message": commit_message,
                                }
                            )
                            logger.info(f"Successfully committed deployment deletion to {repository_name}")
                        else:
                            deletion_results["operations"].append(
                                {
                                    "type": "deployment_folder_deletion",
                                    "target": deployment_folder_path,
                                    "repository": repository_name,
                                    "cluster": cluster,
                                    "status": "not_found",
                                }
                            )
                            logger.info(f"Deployment folder not found: {deployment_folder_path}")

                except Exception as e:
                    deletion_results["operations"].append(
                        {
                            "type": "deployment_folder_deletion",
                            "repository": repository_name,
                            "status": "error",
                            "error": str(e),
                        }
                    )
                    deletion_results["errors"].append(f"Error deleting deployment folder from {repository_name}: {e}")
                    logger.exception("Error deleting deployment folder")
                    manifest_folder_removed = False

            self._end_step(stap_manifesten, self._new_errors(deletion_results, fouten_voor_manifesten))

            # Step 8: Remove deployment from project file (idempotent + conflict-safe).
            # Deleting a deployment that is already gone (e.g. a concurrent delete won
            # the git race) is success, not an error; on a push conflict we re-read the
            # remote and re-apply instead of failing with "manual intervention required".
            #
            # Alleen als git leeg is voor deze deployment (RC-254): anders geeft stap 8 een
            # 404 en kan een retry het niet meer repareren. Het argo-bestand staat hier niet
            # meer bij als voorwaarde: stap 2 gooit door als zijn commit mislukt (RC-265).
            #
            # En alleen als de cluster-delete BEVESTIGD is. Het projectbestand is de
            # gewenste staat die elke latere run leest, dus een verwijdering die gecommit
            # wordt terwijl de resources nog staan laat niemand aan beide kanten nog om de
            # verwijdering vragen. De entry laten staan houdt de delete herhaalbaar.
            #
            # delete_deployment_from_yaml_change heeft met opzet geen tegenhanger van deze
            # stap: daar heeft de gebruiker de entry zelf weggehaald, dus het bestand is de
            # INVOER en er is niets om terug te schrijven.
            #
            # project_file_settled is laag 1 van de hermeting hieronder: die meet alleen het
            # cluster, en zou een met opzet bewaarde entry anders als geslaagd melden.
            project_file_settled = False
            if not manifest_folder_removed or not argocd_app_deleted:
                if not manifest_folder_removed:
                    reason = "deployment manifests still in git"
                else:
                    reason = "ArgoCD application not confirmed deleted"
                deletion_results["operations"].append(
                    {
                        "type": "project_file_update",
                        "target": f"deployment '{deployment_name}'",
                        "action": "removed_from_project_file",
                        "status": "skipped",
                        "reason": f"{reason} - deployment kept in the project file so a retry can finish it",
                    }
                )
                logger.warning(
                    "Deployment '%s' blijft in het projectbestand van '%s': %s",
                    deployment_name,
                    project_name,
                    reason,
                )
                if not argocd_app_deleted:
                    # De gitfouten hierboven melden zich zelf al als fout; een onbevestigde
                    # cluster-delete deed dat niet, en zonder fout eindigt de taak groen.
                    deletion_results["errors"].append(
                        f"Deployment '{deployment_name}' was kept in the project file: the ArgoCD "
                        f"application was not confirmed deleted"
                    )
            else:
                stap = self._begin_step("Deployment uit het projectbestand verwijderen", subject=deployment_name)
                fouten_voor = len(deletion_results["errors"])
                try:
                    logger.info(
                        f"Removing deployment '{deployment_name}' from project file for project '{project_name}'"
                    )

                    def _remove_deployment(project_data: dict[str, Any]) -> dict[str, Any] | None:
                        deployments = project_data.get("deployments", []) or []
                        remaining = [dep for dep in deployments if dep.get("name") != deployment_name]
                        if len(remaining) == len(deployments):
                            return None  # already absent -> idempotent no-op
                        project_data["deployments"] = remaining
                        return project_data

                    committed = await self.project_manager.mutate_and_commit_project(
                        _remove_deployment,
                        f"Delete deployment '{deployment_name}' from project {project_name}",
                    )

                    deletion_results["operations"].append(
                        {
                            "type": "project_file_update",
                            "target": f"deployment '{deployment_name}'",
                            "action": "removed_from_project_file" if committed else "already_absent",
                            "status": "success",
                        }
                    )
                    logger.info("Deployment '%s' removed from project file (committed=%s)", deployment_name, committed)
                    project_file_settled = True

                except Exception as e:
                    deletion_results["operations"].append(
                        {
                            "type": "project_file_update",
                            "target": f"deployment '{deployment_name}'",
                            "action": "removed_from_project_file",
                            "status": "error",
                            "error": str(e),
                        }
                    )
                    deletion_results["errors"].append(f"Error removing deployment from project file: {e}")
                    logger.exception(f"Error removing deployment '{deployment_name}' from project file")
                self._end_step(stap, self._new_errors(deletion_results, fouten_voor))

            # Clean up subdomain registration for this deployment
            stap = self._begin_step("Subdomein opruimen", subject=deployment_name)
            fouten_voor = len(deletion_results["errors"])
            try:
                subdomain_connector = SubdomainConnector()
                deleted_subdomains = await subdomain_connector.delete_by_deployment(project_name, deployment_name)
                if deleted_subdomains:
                    deletion_results["operations"].append(
                        {
                            "type": "subdomain_cleanup",
                            "target": f"deployment '{project_name}/{deployment_name}'",
                            "status": "success",
                            "count": deleted_subdomains,
                            "message": f"Deleted {deleted_subdomains} subdomain registration(s)",
                        }
                    )
                    logger.info(
                        f"Deleted {deleted_subdomains} subdomain registration(s) for deployment "
                        f"'{project_name}/{deployment_name}'"
                    )
                else:
                    logger.debug(f"No subdomain registrations found for deployment '{project_name}/{deployment_name}'")
            except Exception as e:
                error_msg = f"Error cleaning up subdomain registrations for deployment: {e}"
                deletion_results["errors"].append(error_msg)
                deletion_results["operations"].append({"type": "subdomain_cleanup", "status": "error", "error": str(e)})
                logger.warning(error_msg)
                # Don't fail the deletion for subdomain cleanup errors
            self._end_step(stap, self._new_errors(deletion_results, fouten_voor))

            # De uitkomst komt van een HERMETING van de lagen, niet van de foutenlijst van
            # de rit: een fout die tijdens de verwijdering waar was en aan het eind niet
            # meer, beschrijft niets dat is achtergebleven (geval 4). Omgekeerd maakt een
            # lege foutenlijst een namespace die blijft staan niet weg (geval 5). Kon de
            # meting niet worden gedaan, dan blijft de foutenlijst het antwoord: onbekend
            # is geen bewijs, in geen van beide richtingen.
            #
            # De meting beslist alleen over de lagen die ze meet. Laag 1, de entry in het
            # projectbestand, staat er niet bij: stap 8 laat die met opzet staan zodra git
            # of het cluster niet schoon was, en dat is geen geslaagde verwijdering.
            stap = self._begin_step("Controleren dat alles verwijderd is", subject=deployment_name)
            fouten_voor = len(deletion_results["errors"])
            if appproject_left_git:
                # Zelfde AppProject-wacht als in _judge_and_delete_project_file.
                await self.project_manager._kubectl_connector.wait_for_resource_absence(
                    "appproject",
                    generate_argocd_appproject_prefix(project_name, current_base_namespace),
                    get_argo_namespace(cluster),
                )
            remaining = await self._measure_remaining(
                cluster,
                applications=[app_name],
                appprojects=[generate_argocd_appproject_prefix(project_name, current_base_namespace)]
                if appproject_left_git
                else [],
                namespaces=[namespace] if namespace_should_be_gone else [],
            )
            if remaining.decides:
                settled = not remaining.present and project_file_settled
                deletion_results["success"] = settled
                for leftover in remaining.present:
                    deletion_results["errors"].append(
                        f"Deployment '{deployment_name}' of project '{project_name}' is not fully deleted: "
                        f"{leftover} still stands"
                    )
                deletion_results["operations"].append(
                    {
                        "type": "deletion_remeasurement",
                        "target": f"deployment '{deployment_name}'",
                        "status": "clean" if settled else "incomplete",
                        "remaining": remaining.present,
                        "project_file_settled": project_file_settled,
                        # De fouten van de rit blijven staan als uitleg bij een schone
                        # eindtoestand; ze beslissen niets meer.
                        "explained_errors": deletion_results["errors"] if settled else [],
                    }
                )
            else:
                deletion_results["success"] = len(deletion_results["errors"]) == 0
                deletion_results["operations"].append(
                    {
                        "type": "deletion_remeasurement",
                        "target": f"deployment '{deployment_name}'",
                        "status": "unreadable",
                        "unreadable": remaining.unreadable,
                    }
                )
            self._end_step(stap, self._new_errors(deletion_results, fouten_voor))
            if force and deletion_results["errors"]:
                logger.warning(
                    f"Force mode finished with {len(deletion_results['errors'])} error(s) for "
                    f"{project_name}/{deployment_name}"
                )

            logger.info(
                f"Deployment deletion completed for {project_name}/{deployment_name} - "
                f"Success: {deletion_results['success']}, Force: {force}, Errors: {len(deletion_results['errors'])}"
            )
            return deletion_results

        except HTTPException as http_error:
            if force:
                # A 404 means the deployment is already absent from desired state (e.g. not in
                # the project file) - that is a successful, idempotent delete, not an error.
                # Reporting it as a failure made the nightly cleaner retry zombie references
                # forever. Any other HTTP error is a real failure and must be reported so the
                # caller / cleaner retries instead of treating the delete as done.
                if http_error.status_code == 404:
                    logger.info(
                        f"Deployment {project_name}/{deployment_name} already absent "
                        f"({http_error.detail}) - treating as deleted"
                    )
                    deletion_results["success"] = True
                    # Idempotent, and visibly so: the caller asked to remove something
                    # that was not there, and gets told that instead of "deleted".
                    deletion_results["already_absent"] = True
                    deletion_results["operations"].append(
                        {
                            "type": "deployment_deletion",
                            "target": deployment_name,
                            "status": "not_found",
                            "reason": str(http_error.detail),
                        }
                    )
                    return deletion_results
                deletion_results["success"] = False
                deletion_results["errors"].append(f"HTTP error during deployment deletion (force mode): {http_error}")
                logger.warning(f"HTTP error during force deletion for {project_name}/{deployment_name}, continuing")
                return deletion_results
            raise
        except Exception as e:
            deletion_results["success"] = False
            deletion_results["errors"].append(f"Critical error during deployment deletion: {e}")
            logger.exception(f"Critical error during deployment deletion for {project_name}/{deployment_name}")
            if force:
                # In force mode, return results with error instead of raising
                logger.warning("Force mode: returning results with critical error instead of raising")
                return deletion_results
            raise HTTPException(status_code=500, detail=f"Critical error during deployment deletion: {e!s}")

    async def delete_deployment_from_yaml_change(
        self,
        project_name: str,
        deployment_data: dict[str, Any],
        project_data: dict[str, Any],
        marked_for_deletion_service: MarkedForDeletionService | None = None,
        previous_yaml: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Delete deployment resources detected as removed from project YAML.

        Unlike ``delete_deployment()``, this method:
        - Receives the deployment data directly (since it no longer exists in current YAML).
        - Marks persistent data resources (databases, buckets) for deferred deletion
          instead of deleting them immediately (when a service is provided).
        - Skips removing the deployment from the project file (already done by the user).

        Ephemeral/infrastructure resources (ArgoCD apps, keycloak clients, secrets,
        manifests, subdomain registrations) are deleted immediately.

        Args:
            project_name: Name of the project.
            deployment_data: The deployment dict from the *previous* YAML.
            project_data: The *current* project YAML (deployment already removed).
            marked_for_deletion_service: Service for marking persistent resources.
                If None, persistent resources are deleted immediately (legacy behavior).
            previous_yaml: The *previous* project YAML for reliable service detection.
                If None, falls back to legacy detection from deployment_data.

        Returns:
            Dictionary containing deletion results and status.
        """
        deployment_name = deployment_data.get("name", "unknown")
        cluster = deployment_data.get("cluster", "")
        base_namespace = deployment_data.get("namespace")
        namespace_used_by_others = _namespace_used_by_others(
            project_data.get("deployments", []), deployment_name, cluster, base_namespace
        )

        deletion_results: dict[str, Any] = {
            "project": project_name,
            "deployment": deployment_name,
            "trigger": "yaml_change",
            "operations": [],
            "success": True,
            "errors": [],
            "service_results": {},
        }

        logger.info(
            "Processing YAML-detected deployment removal: %s/%s (cluster=%s)",
            project_name,
            deployment_name,
            cluster,
        )

        # --- Ephemeral resource cleanup (immediate) ---

        # 1. Delete ArgoCD application file from GitOps
        try:
            gitops_workspace = await self.project_manager.get_argo_workspace()
            argocd_app_file_path = generate_gitops_argocd_application_path(cluster, project_name, deployment_name)
            working_dir = await gitops_workspace.get_working_dir()
            project_dir = os.path.join(working_dir, cluster, project_name)
            file_full_path = os.path.join(working_dir, argocd_app_file_path)

            repository_name = deployment_data.get("repository")
            repo_used_by_others = repository_name is not None and any(
                other_dep.get("name") != deployment_name and other_dep.get("repository") == repository_name
                for other_dep in project_data.get("deployments", [])
            )

            async def _verwijder_argocd_bestanden() -> None:
                if os.path.exists(file_full_path):
                    os.remove(file_full_path)
                    deletion_results["operations"].append(
                        {
                            "type": "argocd_application_file_deletion",
                            "target": argocd_app_file_path,
                            "status": "success",
                        }
                    )
                    logger.info("Deleted ArgoCD application file: %s", argocd_app_file_path)
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "argocd_application_file_deletion",
                            "target": argocd_app_file_path,
                            "status": "not_found",
                        }
                    )

                if base_namespace:
                    _remove_appproject_file(project_dir, project_name, base_namespace, namespace_used_by_others)

                if repository_name and not repo_used_by_others:
                    unique_repo_name = f"{project_name}-{repository_name}"
                    for template in ["argo-repository-https.yaml.jinja", "argo-repository.yaml.jinja"]:
                        filename = get_output_filename_from_template(template, unique_repo_name)
                        file_path = os.path.join(project_dir, filename)
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            logger.info("Deleted repository file: %s", filename)

                # Rebuild kustomization NA de verwijderingen: ervoor noemde hij nog de
                # AppProject en de repository-secrets die er daarna niet meer waren.
                if os.path.isdir(project_dir):
                    self.project_manager._manifest_generator.create_kustomization_files(
                        output_dir=project_dir,
                        namespace=get_argo_namespace(cluster),
                    )

            # Commit GitOps changes
            commit_message = (
                f"Remove ArgoCD resources for deleted deployment '{deployment_name}' from project '{project_name}'"
            )
            await gitops_workspace.mutate_and_push(commit_message, _verwijder_argocd_bestanden)
            deletion_results["operations"].append(
                {
                    "type": "gitops_commit",
                    "status": "success",
                    "message": commit_message,
                }
            )

        except Exception as e:
            deletion_results["errors"].append(f"Error cleaning up ArgoCD resources: {e}")
            deletion_results["operations"].append(
                {
                    "type": "argocd_cleanup",
                    "status": "error",
                    "error": str(e),
                }
            )
            logger.exception("Error cleaning up ArgoCD resources for %s/%s", project_name, deployment_name)

        # 2. Wait for ArgoCD application deletion, then handle namespace
        try:
            argo_connector = create_argo_connector()
            app_name = generate_argocd_application_name(project_name, deployment_name)
            try:
                app_exists = await argo_connector.application_exists(app_name)
            except PermissionError:
                # ArgoCD returns 403 for an application that is already gone (and when
                # it is stalled). This pre-check must not treat that as an error: it
                # would log a traceback and mark a successful deletion as failed, which
                # is exactly what happened for a deployment whose app WAS deleted. Fall
                # through to the deletion wait, which resolves the 403 against the
                # Kubernetes API as ground truth.
                logger.info(
                    "ArgoCD returned permission denied for '%s' during the pre-check; the app is likely "
                    "already gone. Confirming via the deletion wait (Kubernetes API).",
                    app_name,
                )
                app_exists = True

            if app_exists:
                await argo_connector.refresh_application("user-applications")
                await self._terminate_application_operation(app_name, deletion_results)
                argocd_app_deleted = await argo_connector.wait_for_application_deletion(
                    app_name,
                    max_retries=40,
                    retry_delay=5,
                    kubectl_connector=self.project_manager._kubectl_connector,
                    progress_window_seconds=deletion_progress_window(),
                )
                deletion_results["operations"].append(
                    {
                        "type": "argocd_app_deletion_wait",
                        "target": app_name,
                        "status": "success" if argocd_app_deleted else "timeout",
                    }
                )
            else:
                argocd_app_deleted = True
                deletion_results["operations"].append(
                    {
                        "type": "argocd_app_deletion_wait",
                        "target": app_name,
                        "status": "not_found",
                    }
                )
        except Exception as e:
            argocd_app_deleted = False
            deletion_results["errors"].append(f"Error waiting for ArgoCD app deletion: {e}")
            logger.exception("Error waiting for ArgoCD app deletion: %s", app_name)

        # --- Service resource cleanup ---
        # Use deployment_uses_service for reliable detection when previous_yaml
        # is available; fall back to legacy heuristic otherwise.
        # We pass previous_yaml to managers so their internal service checks pass
        # (the deployment no longer exists in current_yaml).
        file_handler = self.project_manager._project_file_handler
        service_check_yaml = previous_yaml if previous_yaml is not None else project_data

        if previous_yaml is not None:
            has_database = file_handler.deployment_uses_service(
                previous_yaml,
                deployment_name,
                [ServiceType.POSTGRESQL_DATABASE.value, ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value],
            )
            has_minio = file_handler.deployment_uses_service(
                previous_yaml,
                deployment_name,
                [ServiceType.MINIO_STORAGE.value],
            )
            has_seaweedfs = file_handler.deployment_uses_service(
                previous_yaml,
                deployment_name,
                [ServiceType.SEAWEEDFS_STORAGE.value],
            )
            has_redis = file_handler.deployment_uses_service(
                previous_yaml,
                deployment_name,
                [ServiceType.REDIS.value, ServiceType.NAMESPACE_REDIS.value],
            )
            has_send_email = file_handler.deployment_uses_service(
                previous_yaml,
                deployment_name,
                [ServiceType.SEND_EMAIL.value],
            )
        else:
            # Legacy fallback: unreliable but kept for backward compatibility. Resolve
            # entries via the canonical helper (bare string / legacy / record) and match
            # the same canonical service types the primary path uses, instead of reading
            # only ``reference`` against non-canonical name literals.
            services = deployment_data.get("services", [])
            service_names = {service_entry_name(s) for s in services}
            has_database = bool(
                service_names & {ServiceType.POSTGRESQL_DATABASE.value, ServiceType.NAMESPACE_POSTGRESQL_DATABASE.value}
            )
            has_minio = ServiceType.MINIO_STORAGE.value in service_names
            has_seaweedfs = ServiceType.SEAWEEDFS_STORAGE.value in service_names
            has_redis = bool(service_names & {ServiceType.REDIS.value, ServiceType.NAMESPACE_REDIS.value})
            has_send_email = ServiceType.SEND_EMAIL.value in service_names

        # 3. Delete Keycloak resources (ephemeral)
        # Uses service_check_yaml so the deployment's component references are found
        try:
            keycloak_results = await self.project_manager._keycloak_manager.delete_resources_for_deployment(
                service_check_yaml, deployment_data
            )
            deletion_results["service_results"]["keycloak"] = keycloak_results
            deletion_results["operations"].extend(keycloak_results.get("operations", []))
            if keycloak_results.get("errors"):
                deletion_results["errors"].extend(keycloak_results["errors"])
        except Exception as e:
            deletion_results["errors"].append(f"Error deleting Keycloak resources: {e}")
            logger.exception("Error deleting Keycloak resources for %s/%s", project_name, deployment_name)

        # 3b. Delete Redis resources (ephemeral)
        if has_redis:
            try:
                redis_result = await self.project_manager._redis_manager.handle_service_removal(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    deployment_data=deployment_data,
                    project_data=service_check_yaml,
                )
                deletion_results["service_results"]["redis"] = redis_result
                deletion_results["operations"].extend(redis_result.get("operations", []))
                if redis_result.get("errors"):
                    deletion_results["errors"].extend(redis_result["errors"])
            except Exception as e:
                deletion_results["errors"].append(f"Error deleting Redis resources: {e}")
                logger.exception("Error deleting Redis resources for %s/%s", project_name, deployment_name)

        # Delegate persistent resources to managers via handle_service_removal
        if has_database:
            try:
                db_manager = await self.project_manager._ensure_database_manager()
                db_result = await db_manager.handle_service_removal(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    deployment_data=deployment_data,
                    project_data=service_check_yaml,
                    marked_for_deletion_service=marked_for_deletion_service,
                )
                deletion_results["service_results"]["database"] = db_result
                deletion_results["operations"].extend(db_result.get("operations", []))
                if db_result.get("errors"):
                    deletion_results["errors"].extend(db_result["errors"])
            except Exception as e:
                deletion_results["errors"].append(f"Error handling database service removal: {e}")
                logger.exception("Error handling database service removal")

        if has_minio:
            try:
                minio_result = await self.project_manager._minio_manager.handle_service_removal(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    deployment_data=deployment_data,
                    project_data=service_check_yaml,
                    marked_for_deletion_service=marked_for_deletion_service,
                )
                deletion_results["service_results"]["minio"] = minio_result
                deletion_results["operations"].extend(minio_result.get("operations", []))
                if minio_result.get("errors"):
                    deletion_results["errors"].extend(minio_result["errors"])
            except Exception as e:
                deletion_results["errors"].append(f"Error handling MinIO service removal: {e}")
                logger.exception("Error handling MinIO service removal")

        # 3c. Het mailaccount van deze deployment (RC-401): van de deployment, dus het gaat
        # mee met de deployment. Vluchtig zoals Keycloak en Redis -- er is geen data om te
        # bewaren, alleen een account met een wachtwoord dat nergens anders meer bestaat.
        if has_send_email:
            try:
                mail_result = await self.project_manager._mail_manager.handle_service_removal(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    deployment_data=deployment_data,
                    project_data=service_check_yaml,
                )
                deletion_results["service_results"]["send_email"] = mail_result
                deletion_results["operations"].extend(mail_result.get("operations", []))
                if mail_result.get("errors"):
                    deletion_results["errors"].extend(mail_result["errors"])
            except Exception as e:
                deletion_results["errors"].append(f"Error deleting send-email resources: {e}")
                logger.exception("Error deleting send-email resources for %s/%s", project_name, deployment_name)

        if has_seaweedfs:
            try:
                seaweedfs_result = await self.project_manager._seaweedfs_manager.handle_service_removal(
                    project_name=project_name,
                    deployment_name=deployment_name,
                    deployment_data=deployment_data,
                    project_data=service_check_yaml,
                    marked_for_deletion_service=marked_for_deletion_service,
                )
                deletion_results["service_results"]["seaweedfs"] = seaweedfs_result
                deletion_results["operations"].extend(seaweedfs_result.get("operations", []))
                if seaweedfs_result.get("errors"):
                    deletion_results["errors"].extend(seaweedfs_result["errors"])
            except Exception as e:
                deletion_results["errors"].append(f"Error handling SeaweedFS service removal: {e}")
                logger.exception("Error handling SeaweedFS service removal")

        has_deferred = has_database or has_minio or has_seaweedfs

        if marked_for_deletion_service is not None:
            # Mark backup data for deferred deletion
            if base_namespace:
                from opi.manager.backup.base import BackupConfig, get_backup_bucket_name

                namespace = get_prefixed_namespace(cluster, base_namespace)
                backup_bucket = get_backup_bucket_name(project_name, cluster)
                backup_prefix = generate_backup_prefix(cluster, namespace)
                backup_resource_name = f"{backup_bucket}/{backup_prefix}"

                # Derive the Kopia password now while the namespace still exists.
                # Store it in metadata so the reconciliation job can connect to the
                # Kopia repository later, even after the namespace has been deleted.
                backup_config = BackupConfig.from_settings()
                from opi.manager.backup.base import BaseBackupManager

                backup_mgr = BaseBackupManager(config=backup_config)
                try:
                    kopia_password = await backup_mgr._derive_backup_key(namespace)
                except Exception as e:
                    logger.warning(
                        "Could not derive Kopia password for %s (backups may require manual cleanup): %s",
                        namespace,
                        e,
                    )
                    kopia_password = None

                await marked_for_deletion_service.mark_resource(
                    resource_type="backup_data",
                    resource_name=backup_resource_name,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    cluster=cluster,
                    metadata={
                        "s3_bucket": backup_bucket,
                        "s3_prefix": backup_prefix,
                        "kopia_password": kopia_password,
                        "namespace": namespace,
                    },
                )
                deletion_results["operations"].append(
                    {
                        "type": "mark_for_deletion",
                        "resource_type": "backup_data",
                        "resource_name": backup_resource_name,
                        "status": "marked",
                    }
                )

            # Handle namespace: mark instead of deleting if it has persistent resources
            if base_namespace and not namespace_used_by_others:
                namespace = get_prefixed_namespace(cluster, base_namespace)
                if has_deferred:
                    await marked_for_deletion_service.mark_resource(
                        resource_type="namespace",
                        resource_name=namespace,
                        project_name=project_name,
                        deployment_name=deployment_name,
                        cluster=cluster,
                        metadata={"has_marked_pvcs": has_deferred},
                    )
                    deletion_results["operations"].append(
                        {
                            "type": "mark_for_deletion",
                            "resource_type": "namespace",
                            "resource_name": namespace,
                            "status": "marked",
                        }
                    )
                elif argocd_app_deleted:
                    ns_deleted = await self.project_manager._kubectl_connector.delete_namespace(namespace)
                    deletion_results["operations"].append(
                        {
                            "type": "namespace_deletion",
                            "target": namespace,
                            "status": "success" if ns_deleted else "not_found",
                        }
                    )
        else:
            # No marking service: delete namespace if safe (legacy)
            if base_namespace and not namespace_used_by_others and argocd_app_deleted:
                namespace = get_prefixed_namespace(cluster, base_namespace)
                ns_deleted = await self.project_manager._kubectl_connector.delete_namespace(namespace)
                deletion_results["operations"].append(
                    {
                        "type": "namespace_deletion",
                        "target": namespace,
                        "status": "success" if ns_deleted else "not_found",
                    }
                )

        # 4. Delete deployment manifests from git
        # IMPORTANT: Only delete manifests if ArgoCD app is confirmed deleted.
        # If the app still exists, its resources-finalizer needs the source path
        # to determine which K8s resources to clean up. Deleting the manifests
        # while the finalizer is still running causes a permanent deadlock.
        if not argocd_app_deleted:
            logger.warning(
                "Skipping deployment manifest deletion for %s/%s - "
                "ArgoCD application not confirmed deleted. Marking for deferred cleanup.",
                project_name,
                deployment_name,
            )
            try:
                from opi.services.marked_for_deletion_service import MarkedForDeletionService as MFDService

                # MFDService is ORM-backed and takes no constructor arguments.
                # Passing a pool raised TypeError, which the except below swallowed,
                # so manifests were never actually marked for deferred cleanup.
                deferred_service = MFDService()
                resource_name = f"{cluster}/{project_name}/{deployment_name}"
                app_name_for_mark = generate_argocd_application_name(project_name, deployment_name)
                await deferred_service.mark_resource(
                    resource_type="deployment_manifests",
                    resource_name=resource_name,
                    project_name=project_name,
                    deployment_name=deployment_name,
                    cluster=cluster,
                    metadata={
                        "repository_name": repository_name,
                        "argocd_app_name": app_name_for_mark,
                    },
                )
                deletion_results["operations"].append(
                    {
                        "type": "deployment_folder_deletion",
                        "status": "deferred",
                        "reason": "ArgoCD application not confirmed deleted - marked for retry",
                    }
                )
            except Exception as mark_err:
                logger.warning(
                    "Could not mark manifests for deferred deletion: %s. "
                    "Manifests will remain in git until manually cleaned up.",
                    mark_err,
                )
                deletion_results["operations"].append(
                    {
                        "type": "deployment_folder_deletion",
                        "status": "skipped",
                        "reason": "ArgoCD app not deleted and could not mark for retry",
                    }
                )
        elif repository_name and cluster:
            try:
                repositories = project_data.get("repositories", [])
                # Also check previous project data repositories
                prev_repositories = deployment_data.get("_project_repositories", repositories)
                repo_config = None
                for repo in repositories or prev_repositories:
                    if repo.get("name") == repository_name:
                        repo_config = repo
                        break

                if repo_config:
                    workspace = await self.project_manager.get_deployment_workspace(repository_name, repo_config)
                    repo_path = repo_config.get("path", "")
                    deployment_folder_path = generate_deployment_manifest_path(
                        cluster, project_name, deployment_name, repo_path
                    )
                    commit_message = f"Delete deployment '{deployment_name}' from project '{project_name}'"

                    if await workspace.remove_and_push(deployment_folder_path, commit_message):
                        deletion_results["operations"].append(
                            {
                                "type": "deployment_folder_deletion",
                                "target": deployment_folder_path,
                                "status": "success",
                            }
                        )
                    else:
                        deletion_results["operations"].append(
                            {
                                "type": "deployment_folder_deletion",
                                "target": deployment_folder_path,
                                "status": "not_found",
                            }
                        )
            except Exception as e:
                deletion_results["errors"].append(f"Error deleting deployment manifests: {e}")
                logger.exception("Error deleting deployment manifests")

        # 5. Clean up subdomain registrations
        try:
            subdomain_connector = SubdomainConnector()
            deleted_subdomains = await subdomain_connector.delete_by_deployment(project_name, deployment_name)
            if deleted_subdomains:
                deletion_results["operations"].append(
                    {
                        "type": "subdomain_cleanup",
                        "status": "success",
                        "count": deleted_subdomains,
                    }
                )
        except Exception as e:
            deletion_results["errors"].append(f"Error cleaning up subdomain registrations: {e}")
            logger.warning("Error cleaning up subdomain registrations: %s", e)

        deletion_results["success"] = len(deletion_results["errors"]) == 0

        logger.info(
            "YAML-detected deployment deletion completed for %s/%s - Success: %s, Errors: %d",
            project_name,
            deployment_name,
            deletion_results["success"],
            len(deletion_results["errors"]),
        )
        return deletion_results

    # -- Manager-key → manager instance resolution ------------------------
    # The service-type → manager-key mapping now lives on each provider as
    # `cleanup_manager_key` (RC-5 Phase 5); this only resolves a key to its
    # manager instance, invoked via RemovalContext.get_manager.

    async def _get_manager_for_service(self, manager_key: ManagerKey) -> Any:
        """Resolve the manager instance for a given manager key.

        Database is special (an async ensure); the rest are plain attributes, so a
        dict lookup replaces the old if-chain. The enum makes the set exhaustive at
        type-check time -- a bad key is a pyright error, not a teardown-time crash.
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

        # Only check deployments that survive (exist in both)
        surviving = set(previous_deployments) & set(current_deployments)
        results["deployments_checked"] = len(surviving)

        if not surviving:
            logger.debug("No surviving deployments to check for service removal")
            return results

        # Get all service types that require cleanup
        cleanable_services = ServiceAdapter.get_cleanable_service_types()

        file_handler = self.project_manager._project_file_handler

        for dep_name in sorted(surviving):
            prev_dep_data = previous_deployments[dep_name]

            for svc_type in cleanable_services:
                svc_values = [svc_type.value]
                # Group related service types that share a manager
                # (e.g., namespace-postgresql-database is handled by database manager)
                # We only need to check once per manager per deployment
                # RC-5 Phase 5: dispatch cleanup through the provider registry instead
                # of the _SERVICE_TYPE_MANAGER_ATTR map. Byte-identical -- the provider
                # resolves the same manager by key and delegates handle_service_removal.
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
