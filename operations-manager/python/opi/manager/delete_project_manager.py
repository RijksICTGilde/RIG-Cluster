"""Project deletion manager for handling project and deployment deletion operations."""

from __future__ import annotations

import logging
import os
import shutil
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from opi.connectors import create_argo_connector
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
from opi.utils.naming import generate_project_admin_username, generate_project_realm_name

if TYPE_CHECKING:
    from opi.services.marked_for_deletion_service import MarkedForDeletionService
from opi.utils.naming import (
    generate_argocd_application_name,
    generate_argocd_appproject_prefix,
    generate_backup_prefix,
    generate_deployment_manifest_path,
    generate_gitops_argocd_application_path,
    generate_infrastructure_application_name,
    generate_infrastructure_argocd_folder_path,
    get_output_filename_from_template,
)

logger = logging.getLogger(__name__)


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


class DeleteProjectManager:
    """Manager for project and deployment deletion operations."""

    def __init__(self, project_manager: ProjectManager) -> None:
        """
        Initialize the DeleteProjectManager with reference to ProjectManager.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
        """
        self.project_manager = project_manager

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
                        delete_success = await argo_connector.delete_application(app_name)
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
            argo_namespace = get_argo_namespace(settings.CLUSTER_MANAGER)

            # Use kubectl to list AppProjects matching the pattern
            kubectl = self.project_manager._kubectl_connector
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

    async def _delete_project_argocd_folder(
        self, project_name: str, cluster: str, deletion_results: dict[str, Any], expect_folder: bool = False
    ) -> None:
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
            gitops_connector = await self.project_manager.get_git_connector_for_argocd()
            # Verversen en niet alleen klonen: de connector is gecached en ensure_repo_cloned
            # fetcht hooguit eenmaal per proces, terwijl we hieronder een BESLISSING nemen op
            # wat er op schijf staat.
            await gitops_connector.refresh_working_tree()
            working_dir = await gitops_connector.get_working_dir()
            project_argocd_folder = os.path.join(working_dir, project_argocd_folder_rel)

            if os.path.exists(project_argocd_folder):
                shutil.rmtree(project_argocd_folder)
                deletion_results["operations"].append(
                    {
                        "type": "project_argocd_folder_deletion",
                        "target": project_argocd_folder_rel,
                        "status": "success",
                    }
                )
                logger.info(f"Deleted project ArgoCD folder: {project_argocd_folder_rel}")

                commit_message = f"Delete ArgoCD resources for project '{project_name}'"
                await gitops_connector.commit_and_push(commit_message)
                deletion_results["operations"].append(
                    {"type": "project_argocd_gitops_commit", "status": "success", "message": commit_message}
                )

                # Refresh the root application so the orphaned AppProject and
                # repository secret are pruned promptly
                argo_connector = create_argo_connector()
                await argo_connector.refresh_application("user-applications")
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
                await keycloak.delete_user_by_username("master", admin_username)
                logger.info(f"Deleted project admin {admin_username}")
                deletion_results["operations"].append(
                    {"type": "keycloak_user_deletion", "target": admin_username, "status": "success"}
                )
            except Exception as e:
                logger.exception(f"Failed to delete user {admin_username}")
                deletion_results["errors"].append(f"User deletion: {e}")

            # 3. Delete platform client from RIG Platform realm
            try:
                await keycloak.delete_deployment_client(
                    deployment_name=platform_client_id,
                    project_name="",
                    realm_name=settings.KEYCLOAK_DEFAULT_REALM,
                )
                logger.info(f"Deleted platform client {platform_client_id}")
                deletion_results["operations"].append(
                    {"type": "keycloak_platform_client_deletion", "target": platform_client_id, "status": "success"}
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

            gitops_connector = await self.project_manager.get_git_connector_for_argocd()
            await gitops_connector.ensure_repo_cloned()
            working_dir = await gitops_connector.get_working_dir()
            infra_argocd_folder = os.path.join(working_dir, infra_argocd_folder_rel)

            if os.path.exists(infra_argocd_folder):
                shutil.rmtree(infra_argocd_folder)
                deletion_results["operations"].append(
                    {
                        "type": "infrastructure_argocd_folder_deletion",
                        "target": infra_argocd_folder_rel,
                        "status": "success",
                    }
                )
                logger.info(f"Deleted infrastructure ArgoCD folder: {infra_argocd_folder_rel}")

                commit_message = f"Delete infrastructure ArgoCD application for project '{project_name}'"
                await gitops_connector.commit_and_push(commit_message)
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
                deletion_complete = await argo_connector.wait_for_application_deletion(
                    infra_app_name,
                    max_retries=20,
                    kubectl_connector=self.project_manager._kubectl_connector,
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
                            "force mode: attempting to remove finalizers"
                        )
                        kubectl = self.project_manager._kubectl_connector
                        finalizer_removed = await kubectl.remove_argocd_application_finalizers(infra_app_name)
                        if finalizer_removed:
                            deletion_results["operations"].append(
                                {
                                    "type": "infrastructure_app_finalizer_removal",
                                    "target": infra_app_name,
                                    "status": "success",
                                }
                            )
                            infra_app_deleted = await argo_connector.wait_for_application_deletion(
                                infra_app_name,
                                max_retries=10,
                                kubectl_connector=self.project_manager._kubectl_connector,
                            )
                        else:
                            deletion_results["operations"].append(
                                {
                                    "type": "infrastructure_app_finalizer_removal",
                                    "target": infra_app_name,
                                    "status": "failed",
                                }
                            )

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
                manifest_connector = await self.project_manager.get_git_connector_for_deployment(
                    "infrastructure", repo_config
                )

                repo_path = repo_config.get("path", "")
                infra_manifest_path = generate_infrastructure_manifest_path(cluster, project_name, repo_path)
                logger.info(f"Deleting infrastructure manifests folder: {infra_manifest_path}")

                await manifest_connector.ensure_repo_cloned()
                infra_folder_full_path = os.path.join(await manifest_connector.get_working_dir(), infra_manifest_path)

                if os.path.exists(infra_folder_full_path):
                    shutil.rmtree(infra_folder_full_path)
                    deletion_results["operations"].append(
                        {
                            "type": "infrastructure_manifest_deletion",
                            "target": infra_manifest_path,
                            "status": "success",
                        }
                    )
                    logger.info(f"Deleted infrastructure manifests folder: {infra_manifest_path}")

                    commit_message = f"Delete infrastructure manifests for project '{project_name}'"
                    await manifest_connector.commit_and_push_changes(commit_message)
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

    async def delete_project(self, project_name: str, force: bool = False) -> dict[str, Any]:
        """
        Delete a project by first deleting all deployments, then cleaning up project-level resources.

        This method implements the project deletion logic:
        1. Validate no deployments exist on other clusters
        2. Delete all deployments on the current cluster (deployment-level resources only)
        3. Clean up project-level infrastructure (namespace-specific PostgreSQL/Redis)
        4. Clean up project Keycloak realm(s)
        5. Delete the project file
        6. Clean up subdomains and in-memory state

        Args:
            project_name: Name of the project to delete
            force: If True, continues on errors and cleans up stuck resources.
                   Use when a previous deletion failed partially.

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
            for deployment in current_cluster_deployments:
                deployment_name = deployment.get("name")
                logger.info(f"Deleting deployment {deployment_name} from project {project_name}")

                try:
                    deployment_deletion_result = await self.delete_deployment(
                        project_name, deployment_name, force=force
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
                        if not force:
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
                    if not force:
                        deletion_results["success"] = False
                    logger.exception(error_msg)

            # Step 4.5: Clean up project-level infrastructure (namespace-specific PostgreSQL/Redis)
            if deletion_results["success"] or force:
                await self._cleanup_project_infrastructure(
                    project_name, current_cluster, project_data, deletion_results, force
                )

            # Step 4.6: Clean up project Keycloak realm(s)
            if deletion_results["success"] or force:
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

            # Step 4.7: Delete the project's ArgoCD folder (AppProject, repository secret, kustomization)
            # from the GitOps repo, so the root application prunes these resources
            if deletion_results["success"] or force:
                await self._delete_project_argocd_folder(
                    project_name,
                    current_cluster,
                    deletion_results,
                    expect_folder=len(current_cluster_deployments) > 0,
                )

            # Step 5: Delete the project file if all deployment deletions succeeded (or in force mode)
            should_delete_project_file = deletion_results["success"] or force
            if should_delete_project_file and len(current_cluster_deployments) > 0:
                if force and not deletion_results["success"]:
                    logger.info(f"Force mode: deleting project file despite errors for {project_name}")
                else:
                    logger.info(f"All deployments deleted successfully, now deleting project file for {project_name}")

                commit_message = f"Delete project '{project_name}' - removed project file after deployment cleanup"
                delete_result = await self._delete_project_file(project_name, commit_message)

                deletion_results["operations"].extend(delete_result["operations"])
                deletion_results["errors"].extend(delete_result["errors"])
                if not delete_result["success"]:
                    deletion_results["success"] = False

            elif len(current_cluster_deployments) == 0:
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
                delete_result = await self._delete_project_file(project_name, commit_message)

                deletion_results["operations"].extend(delete_result["operations"])
                deletion_results["errors"].extend(delete_result["errors"])
                if not delete_result["success"]:
                    deletion_results["success"] = False

            # Clean up subdomain registrations for this project
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
            await get_project_store().delete(project_name, message=commit_message, actor="delete-project")

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

    async def delete_deployment(self, project_name: str, deployment_name: str, force: bool = False) -> dict[str, Any]:
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
                   - Removes ArgoCD finalizers if app deletion times out
                   - Skips database cleanup if secrets are inaccessible
                   - Only deletes namespace after ArgoCD app is confirmed deleted

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

            # Step 2: Delete ArgoCD application file from GitOps
            logger.info(f"Deleting ArgoCD application file for {project_name}/{deployment_name}")

            try:
                gitops_connector = await self.project_manager.get_git_connector_for_argocd()

                argocd_app_file_path = generate_gitops_argocd_application_path(cluster, project_name, deployment_name)
                logger.info(f"Attempting to delete ArgoCD application file: {argocd_app_file_path}")

                await gitops_connector.ensure_repo_cloned()
                file_full_path = os.path.join(await gitops_connector.get_working_dir(), argocd_app_file_path)
                file_exists = os.path.exists(file_full_path)

                if file_exists:
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

                # Rebuild kustomization file
                logger.info(f"Rebuilding kustomization.yaml for project {project_name} in cluster {cluster}")
                working_dir = await gitops_connector.get_working_dir()
                project_dir = os.path.join(working_dir, cluster, project_name)

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

                # Commit changes to GitOps repository
                commit_message = f"Delete ArgoCD application for deployment '{deployment_name}' from project '{project_name}' and rebuild kustomization"
                await gitops_connector.commit_and_push(commit_message)
                deletion_results["operations"].append(
                    {"type": "gitops_commit", "status": "success", "message": commit_message}
                )

            except Exception as e:
                argocd_app_file_path = generate_gitops_argocd_application_path(cluster, project_name, deployment_name)
                deletion_results["operations"].append(
                    {
                        "type": "argocd_application_file_deletion",
                        "target": argocd_app_file_path,
                        "cluster": cluster,
                        "deployment": deployment_name,
                        "status": "error",
                        "error": str(e),
                    }
                )
                deletion_results["errors"].append(f"Error deleting ArgoCD application file: {e}")
                logger.exception("Error deleting ArgoCD application file")

            # Step 2.1: Delete ArgoCD AppProject file (only if no other deployments use the same namespace)
            current_base_namespace = deployment.get("namespace")
            namespace_used_by_others = any(
                other_dep.get("name") != deployment_name
                and other_dep.get("cluster") == cluster
                and other_dep.get("namespace") == current_base_namespace
                for other_dep in project_data.get("deployments", [])
            )

            if not namespace_used_by_others:
                appproject_name = generate_argocd_appproject_prefix(project_name, current_base_namespace)
                appproject_filename = get_output_filename_from_template("argocd-appproject.yaml.jinja", appproject_name)
                appproject_file_path = os.path.join(
                    await gitops_connector.get_working_dir(), cluster, project_name, appproject_filename
                )
                if os.path.exists(appproject_file_path):
                    os.remove(appproject_file_path)
                    logger.info(f"Deleted AppProject file: {appproject_filename}")

            # Step 2.2: Delete Repository Secret files
            current_repo = deployment.get("repository")
            if current_repo:
                repo_used_by_others = any(
                    other_dep.get("name") != deployment_name and other_dep.get("repository") == current_repo
                    for other_dep in project_data.get("deployments", [])
                )

                if not repo_used_by_others:
                    unique_repo_name = f"{project_name}-{current_repo}"
                    for template in ["argo-repository-https.yaml.jinja", "argo-repository.yaml.jinja"]:
                        filename = get_output_filename_from_template(template, unique_repo_name)
                        file_path = os.path.join(
                            await gitops_connector.get_working_dir(), cluster, project_name, filename
                        )
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            logger.info(f"Deleted repository file: {filename}")

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
                    deletion_complete = await argo_connector.wait_for_application_deletion(
                        app_name,
                        max_retries=40,
                        retry_delay=5,
                        kubectl_connector=self.project_manager._kubectl_connector,
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
                        if force:
                            logger.warning(
                                f"ArgoCD application {app_name} deletion timed out - "
                                "force mode: attempting to remove finalizers"
                            )
                            finalizer_removed = (
                                await self.project_manager._kubectl_connector.remove_argocd_application_finalizers(
                                    app_name
                                )
                            )
                            if finalizer_removed:
                                deletion_results["operations"].append(
                                    {
                                        "type": "argocd_app_finalizer_removal",
                                        "target": app_name,
                                        "status": "success",
                                    }
                                )
                                # Wait for the app to be garbage collected using proper retry logic
                                argocd_app_deleted = await argo_connector.wait_for_application_deletion(
                                    app_name,
                                    max_retries=10,
                                    kubectl_connector=self.project_manager._kubectl_connector,
                                )
                            else:
                                deletion_results["operations"].append(
                                    {
                                        "type": "argocd_app_finalizer_removal",
                                        "target": app_name,
                                        "status": "failed",
                                    }
                                )

                        deletion_results["operations"].append(
                            {
                                "type": "argocd_app_gitops_deletion",
                                "target": app_name,
                                "cluster": cluster,
                                "deployment": deployment_name,
                                "status": "timeout",
                                "error": "Application deletion via GitOps timed out",
                                "finalizer_removed": force and finalizer_removed if force else False,
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

            # Step 5: Delete Kubernetes namespace (only if ArgoCD app was confirmed deleted)
            try:
                base_namespace = deployment.get("namespace", project_name)
                namespace = get_prefixed_namespace(cluster, base_namespace)

                namespace_used_by_others = any(
                    other_dep.get("name") != deployment_name
                    and other_dep.get("cluster") == cluster
                    and other_dep.get("namespace") == base_namespace
                    for other_dep in project_data.get("deployments", [])
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
                    if not argocd_app_deleted:
                        logger.warning(
                            f"Force mode: deleting namespace {namespace} even though ArgoCD app status is uncertain"
                        )
                    logger.info(f"Deleting Kubernetes namespace: {namespace}")
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

            # Step 7: Delete deployment folders from git repositories
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
                        manifest_connector = await self.project_manager.get_git_connector_for_deployment(
                            repository_name, repo_config
                        )

                        repo_path = repo_config.get("path", "")
                        deployment_folder_path = generate_deployment_manifest_path(
                            cluster, project_name, deployment_name, repo_path
                        )
                        logger.info(f"Attempting to delete deployment folder: {deployment_folder_path}")

                        await manifest_connector.ensure_repo_cloned()
                        folder_full_path = os.path.join(
                            await manifest_connector.get_working_dir(), deployment_folder_path
                        )
                        folder_exists = os.path.exists(folder_full_path)

                        if folder_exists:
                            shutil.rmtree(folder_full_path)
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

                            commit_message = f"Delete deployment '{deployment_name}' from project '{project_name}'"
                            commit_result = await manifest_connector.commit_and_push_changes(commit_message)
                            if commit_result:
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
                                deletion_results["errors"].append(
                                    f"Failed to commit deployment changes to {repository_name}"
                                )
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

            # Step 8: Remove deployment from project file (idempotent + conflict-safe).
            # Deleting a deployment that is already gone (e.g. a concurrent delete won
            # the git race) is success, not an error; on a push conflict we re-read the
            # remote and re-apply instead of failing with "manual intervention required".
            try:
                logger.info(f"Removing deployment '{deployment_name}' from project file for project '{project_name}'")

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

            # Clean up subdomain registration for this deployment
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

            # Honest result regardless of mode: a delete that left errors behind is NOT a
            # success. Force mode differs only in that it keeps attempting every step instead
            # of aborting on the first failure - it must NOT relabel a partial deletion as
            # success. (Doing so is what let orphaned previews accumulate: the caller and the
            # nightly cleaner were told "done" while resources stayed behind.)
            deletion_results["success"] = len(deletion_results["errors"]) == 0
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
        namespace_used_by_others = any(
            other_dep.get("name") != deployment_name
            and other_dep.get("cluster") == cluster
            and other_dep.get("namespace") == base_namespace
            for other_dep in project_data.get("deployments", [])
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
            gitops_connector = await self.project_manager.get_git_connector_for_argocd()
            argocd_app_file_path = generate_gitops_argocd_application_path(cluster, project_name, deployment_name)
            await gitops_connector.ensure_repo_cloned()
            file_full_path = os.path.join(await gitops_connector.get_working_dir(), argocd_app_file_path)

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

            # Rebuild kustomization
            working_dir = await gitops_connector.get_working_dir()
            project_dir = os.path.join(working_dir, cluster, project_name)
            if os.path.isdir(project_dir):
                self.project_manager._manifest_generator.create_kustomization_files(
                    output_dir=project_dir,
                    namespace=get_argo_namespace(cluster),
                )

            # Delete AppProject if namespace no longer used by other deployments
            if not namespace_used_by_others and base_namespace:
                appproject_name = generate_argocd_appproject_prefix(project_name, base_namespace)
                appproject_filename = get_output_filename_from_template("argocd-appproject.yaml.jinja", appproject_name)
                appproject_path = os.path.join(working_dir, cluster, project_name, appproject_filename)
                if os.path.exists(appproject_path):
                    os.remove(appproject_path)
                    logger.info("Deleted AppProject file: %s", appproject_filename)

            # Delete repository secret files if not shared
            repository_name = deployment_data.get("repository")
            if repository_name:
                repo_used_by_others = any(
                    other_dep.get("name") != deployment_name and other_dep.get("repository") == repository_name
                    for other_dep in project_data.get("deployments", [])
                )
                if not repo_used_by_others:
                    unique_repo_name = f"{project_name}-{repository_name}"
                    for template in ["argo-repository-https.yaml.jinja", "argo-repository.yaml.jinja"]:
                        filename = get_output_filename_from_template(template, unique_repo_name)
                        file_path = os.path.join(working_dir, cluster, project_name, filename)
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            logger.info("Deleted repository file: %s", filename)

            # Commit GitOps changes
            commit_message = (
                f"Remove ArgoCD resources for deleted deployment '{deployment_name}' from project '{project_name}'"
            )
            await gitops_connector.commit_and_push(commit_message)
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
                argocd_app_deleted = await argo_connector.wait_for_application_deletion(
                    app_name,
                    max_retries=40,
                    retry_delay=5,
                    kubectl_connector=self.project_manager._kubectl_connector,
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
            has_redis = file_handler.deployment_uses_service(
                previous_yaml,
                deployment_name,
                [ServiceType.REDIS.value, ServiceType.NAMESPACE_REDIS.value],
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
            has_redis = bool(service_names & {ServiceType.REDIS.value, ServiceType.NAMESPACE_REDIS.value})

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

        has_deferred = has_database or has_minio

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
                    manifest_connector = await self.project_manager.get_git_connector_for_deployment(
                        repository_name, repo_config
                    )
                    repo_path = repo_config.get("path", "")
                    deployment_folder_path = generate_deployment_manifest_path(
                        cluster, project_name, deployment_name, repo_path
                    )
                    await manifest_connector.ensure_repo_cloned()
                    folder_full_path = os.path.join(await manifest_connector.get_working_dir(), deployment_folder_path)

                    if os.path.exists(folder_full_path):
                        shutil.rmtree(folder_full_path)
                        commit_message = f"Delete deployment '{deployment_name}' from project '{project_name}'"
                        await manifest_connector.commit_and_push_changes(commit_message)
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
