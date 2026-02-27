"""Project deletion manager for handling project and deployment deletion operations."""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

from fastapi import HTTPException

from opi.connectors import create_argo_connector
from opi.connectors.subdomain import SubdomainConnector
from opi.core.cluster_config import get_argo_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.services.project_service import get_project_service
from opi.utils.naming import (
    generate_argocd_application_name,
    generate_argocd_appproject_prefix,
    generate_deployment_manifest_path,
    generate_gitops_argocd_application_path,
    generate_infrastructure_application_name,
    generate_infrastructure_argocd_folder_path,
    get_output_filename_from_template,
)

logger = logging.getLogger(__name__)


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
            appproject_prefix = generate_argocd_appproject_prefix(project_name)
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

    async def _cleanup_project_keycloak_realm(
        self, project_name: str, cluster: str, kc_config: dict[str, Any], deletion_results: dict[str, Any]
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
                keycloak_list = project_data.get("config", {}).get("keycloak", [])

                # Remove entry matching this realm
                updated_list = [kc for kc in keycloak_list if kc.get("realm") != realm_name]

                if updated_list != keycloak_list:
                    project_data["config"]["keycloak"] = updated_list
                    await self.project_manager.save_project_data()
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
        uses_namespace_infrastructure = False
        for service_item in project_services:
            if isinstance(service_item, str):
                if service_item in NAMESPACE_SERVICES:
                    uses_namespace_infrastructure = True
                    break
            elif isinstance(service_item, dict) and any(svc in service_item for svc in NAMESPACE_SERVICES):
                uses_namespace_infrastructure = True
                break

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
                deletion_complete = await argo_connector.wait_for_application_deletion(infra_app_name, max_retries=20)

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
                                infra_app_name, max_retries=10
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
                    deletion_results["operations"].append(
                        {
                            "type": "infrastructure_namespace_deletion",
                            "target": infra_namespace,
                            "status": "not_found",
                        }
                    )

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
        project = get_project_service().get_project(project_name)
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

            # Final step: Remove project from in-memory database if deletion was successful
            if deletion_results["success"]:
                try:
                    project_service = get_project_service()
                    removed = project_service.remove_project(project_name)
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
            git_connector = await self.project_manager.get_git_connector_for_project_files()

            # Look up actual filename from project service (filename may differ from project name)
            project = get_project_service().get_project(project_name)
            if not project:
                result["errors"].append(f"Project '{project_name}' not found in project service")
                result["success"] = False
                return result
            project_file_path = f"projects/{project.filename}"

            await git_connector.ensure_repo_cloned()
            project_file_exists = await git_connector.file_exists(project_file_path)

            if project_file_exists:
                await git_connector.delete_file(project_file_path)
                commit_result = await git_connector.commit_and_push_changes(commit_message)

                if commit_result:
                    result["operations"].append(
                        {
                            "type": "project_file_deletion",
                            "target": project_file_path,
                            "status": "success",
                            "message": commit_message,
                        }
                    )
                    logger.info(f"Successfully deleted project file: {project_file_path}")
                else:
                    result["operations"].append(
                        {
                            "type": "project_file_commit",
                            "status": "failed",
                            "error": "Failed to commit project file deletion",
                        }
                    )
                    result["errors"].append("Failed to commit project file deletion")
                    result["success"] = False
            else:
                result["operations"].append(
                    {"type": "project_file_deletion", "target": project_file_path, "status": "not_found"}
                )
                logger.info(f"Project file {project_file_path} not found (may have been already deleted)")

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

        deletion_results = {
            "project": project_name,
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
            "service_results": {},
            "force_mode": force,
        }

        if force:
            logger.info(f"Force mode enabled for deployment deletion: {project_name}/{deployment_name}")

        # Look up actual filename from project service (filename may differ from project name)
        project = get_project_service().get_project(project_name)
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
                        app_name, max_retries=40, retry_delay=5
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
                                    app_name, max_retries=10
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
                        deletion_results["operations"].append(
                            {
                                "type": "namespace_deletion",
                                "target": namespace,
                                "cluster": cluster,
                                "deployment": deployment_name,
                                "status": "not_found",
                            }
                        )
                        logger.info(f"Namespace {namespace} was not found (already deleted)")
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

            # Delete Keycloak resources
            keycloak_results = await self.project_manager._keycloak_manager.delete_resources_for_deployment(
                project_data, deployment
            )
            deletion_results["service_results"]["keycloak"] = keycloak_results
            deletion_results["operations"].extend(keycloak_results["operations"])
            if keycloak_results["errors"]:
                deletion_results["errors"].extend(keycloak_results["errors"])

            # Delete database resources (using lazy-initialized manager with correct database)
            try:
                db_manager = await self.project_manager._ensure_database_manager()
                database_results = await db_manager.delete_resources_for_deployment(project_data, deployment)
                deletion_results["service_results"]["database"] = database_results
                deletion_results["operations"].extend(database_results["operations"])
                if database_results["errors"]:
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

            # Delete MinIO resources
            minio_results = await self.project_manager._minio_manager.delete_resources_for_deployment(
                project_data, deployment
            )
            deletion_results["service_results"]["minio"] = minio_results
            deletion_results["operations"].extend(minio_results["operations"])
            if minio_results["errors"]:
                deletion_results["errors"].extend(minio_results["errors"])

            # Step 7: Delete deployment folders from git repositories
            logger.info(f"Deleting deployment manifests for {project_name}/{deployment_name}")

            if repository_name and cluster:
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

            # Step 8: Remove deployment from project file
            try:
                logger.info(f"Removing deployment '{deployment_name}' from project file for project '{project_name}'")

                current_project_data = await self.project_manager.get_contents()

                updated_deployments = [
                    dep for dep in current_project_data.get("deployments", []) if dep.get("name") != deployment_name
                ]
                current_project_data["deployments"] = updated_deployments

                await self.project_manager.save_project_data()

                git_connector = await self.project_manager.get_git_connector_for_project_files()
                await git_connector.commit_and_push(
                    f"Delete deployment '{deployment_name}' from project {project_name}"
                )

                deletion_results["operations"].append(
                    {
                        "type": "project_file_update",
                        "target": f"deployment '{deployment_name}'",
                        "action": "removed_from_project_file",
                        "status": "success",
                    }
                )
                logger.info(
                    f"Successfully removed deployment '{deployment_name}' from project file and committed changes"
                )

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

            # Update success status - in force mode, we may still have errors but continue
            if force:
                # In force mode, mark as partial success if there were some errors but we continued
                deletion_results["success"] = True  # Force mode completes even with errors
                if deletion_results["errors"]:
                    logger.info(
                        f"Force mode completed with {len(deletion_results['errors'])} error(s) for "
                        f"{project_name}/{deployment_name}"
                    )
            else:
                deletion_results["success"] = len(deletion_results["errors"]) == 0

            logger.info(
                f"Deployment deletion completed for {project_name}/{deployment_name} - "
                f"Success: {deletion_results['success']}, Force: {force}, Errors: {len(deletion_results['errors'])}"
            )
            return deletion_results

        except HTTPException as http_error:
            if force:
                # In force mode, convert HTTPException to error in results and continue
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
