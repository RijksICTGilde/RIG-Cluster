"""Project deletion manager for handling project and deployment deletion operations."""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

from fastapi import HTTPException

from opi.connectors import create_argo_connector
from opi.core.cluster_config import get_argo_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.services.project_service import get_project_service
from opi.utils.naming import (
    generate_argocd_application_name,
    generate_argocd_appproject_prefix,
    generate_deployment_manifest_path,
    generate_gitops_argocd_application_path,
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

    async def _get_project_keycloak_config_for_cluster(
        self, project_data: dict[str, Any], cluster: str
    ) -> dict[str, Any] | None:
        """
        Find Keycloak config entry for a specific cluster.

        Args:
            project_data: Project configuration dictionary
            cluster: Name of the cluster

        Returns:
            Keycloak config entry with host/realm/username/password or None if not found
        """
        from opi.utils.naming import generate_project_realm_name

        keycloak_list = project_data.get("config", {}).get("keycloak", [])
        if not keycloak_list:
            return None

        project_name = await self.project_manager.get_name()
        expected_realm = generate_project_realm_name(project_name, cluster)

        for entry in keycloak_list:
            if entry.get("realm") == expected_realm:
                return entry

        return None

    def _count_deployments_in_cluster(self, project_data: dict[str, Any], cluster: str) -> int:
        """
        Count deployments for a specific cluster.

        Args:
            project_data: Project configuration dictionary
            cluster: Name of the cluster

        Returns:
            Number of deployments in the specified cluster
        """
        deployments = project_data.get("deployments", [])
        return sum(1 for d in deployments if d.get("cluster") == cluster)

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

    async def delete_deployment_resources(self, project_name: str, deployment_name: str) -> dict[str, Any]:
        """
        Delete resources for a specific deployment.

        Steps:
        1. Get deployment config to find cluster
        2. Get project keycloak config for cluster
        3. Delete deployment client from project realm
        4. Check if this is the last deployment in cluster
        5. If yes, delete project realm/admin/platform-client
        6. Delete GitOps manifests folder
        7. Delete Kubernetes namespace

        Args:
            project_name: Name of the project
            deployment_name: Name of the deployment to delete

        Returns:
            Dictionary containing deletion results and status
        """
        from opi.connectors.keycloak import create_keycloak_connector

        deletion_results = {
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        logger.info(f"Starting deletion of deployment {deployment_name} from project {project_name}")

        try:
            # Get project data
            project_data = await self.project_manager.get_contents()

            # Find deployment config using helper method
            deployment_config = await self.project_manager.get_deployment_by_name(deployment_name)

            if not deployment_config:
                logger.warning(f"Deployment {deployment_name} not found in project {project_name}")
                deletion_results["errors"].append(f"Deployment {deployment_name} not found")
                return deletion_results

            cluster = deployment_config.get("cluster")

            # Get keycloak config for cluster
            kc_config = await self.project_manager._get_project_keycloak_config_for_cluster(cluster)

            if kc_config:
                realm_name = kc_config["realm"]
                keycloak_host = kc_config["host"]

                # Delete deployment client from project realm
                try:
                    logger.info(f"Deleting Keycloak client for deployment {deployment_name} from realm {realm_name}")

                    keycloak = await create_keycloak_connector(
                        keycloak_url=keycloak_host,
                        admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                        admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
                    )

                    delete_success = await keycloak.delete_deployment_client(
                        deployment_name=deployment_name, project_name=project_name, realm_name=realm_name
                    )

                    if delete_success:
                        logger.info(f"Successfully deleted Keycloak client for deployment {deployment_name}")
                        deletion_results["operations"].append(
                            {
                                "type": "keycloak_client_deletion",
                                "target": f"{project_name}-{deployment_name}",
                                "realm": realm_name,
                                "status": "success",
                            }
                        )
                    else:
                        logger.warning(f"Keycloak client for deployment {deployment_name} was not found")
                        deletion_results["operations"].append(
                            {
                                "type": "keycloak_client_deletion",
                                "target": f"{project_name}-{deployment_name}",
                                "realm": realm_name,
                                "status": "not_found",
                            }
                        )

                except Exception as e:
                    logger.exception("Failed to delete Keycloak client")
                    deletion_results["errors"].append(f"Keycloak client deletion: {e}")

                # Check if this is the last deployment in this cluster
                remaining_deployments = self._count_deployments_in_cluster(project_data, cluster)

                if remaining_deployments == 1:  # This deployment is the last one
                    logger.info(f"Last deployment in cluster {cluster}, cleaning up project realm")

                    await self._cleanup_project_keycloak_realm(
                        project_name=project_name,
                        cluster=cluster,
                        kc_config=kc_config,
                        deletion_results=deletion_results,
                    )

            logger.info(f"Completed deletion of deployment {deployment_name}")

        except Exception as e:
            logger.exception(f"Error deleting deployment {deployment_name}")
            deletion_results["success"] = False
            deletion_results["errors"].append(str(e))

        return deletion_results

    async def delete_project(self, project_name: str) -> dict[str, Any]:
        """
        Delete a project by first deleting all deployments on the current cluster.

        This method implements the deployment-aware deletion logic:
        1. Delete all deployments on the current cluster using the deployment delete method
        2. Validate that no deployments remain on other clusters
        3. Only delete the project file itself if no deployments are left anywhere

        Args:
            project_name: Name of the project to delete

        Returns:
            Dictionary containing deletion results and status

        Raises:
            HTTPException: If critical operations fail or deployments exist on other clusters
        """
        deletion_results = {
            "project": project_name,
            "operations": [],
            "success": True,
            "errors": [],
            "deployment_deletions": {},
            "remaining_deployments": [],
        }

        self.project_manager._project_file_relative_path = f"projects/{project_name}.yaml"

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
                    deployment_deletion_result = await self.delete_deployment(project_name, deployment_name)
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
                                "status": "failed",
                                "errors": deployment_deletion_result["errors"],
                            }
                        )
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

            # Step 5: Delete the project file if all deployment deletions succeeded
            if deletion_results["success"] and len(current_cluster_deployments) > 0:
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

                commit_message = f"Delete project '{project_name}' - no deployments remaining"
                delete_result = await self._delete_project_file(project_name, commit_message)

                deletion_results["operations"].extend(delete_result["operations"])
                deletion_results["errors"].extend(delete_result["errors"])
                if not delete_result["success"]:
                    deletion_results["success"] = False

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
            project_file_path = f"projects/{project_name}.yaml"

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

    async def delete_deployment(self, project_name: str, deployment_name: str) -> dict[str, Any]:
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

        Returns:
            Dictionary containing deletion results and status

        Raises:
            HTTPException: If critical operations fail
        """

        deletion_results = {
            "project": project_name,
            "deployment": deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
            "service_results": {},
        }

        self.project_manager._project_file_relative_path = f"projects/{project_name}.yaml"

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
            try:
                app_name = generate_argocd_application_name(project_name, deployment_name)

                app_exists = await argo_connector.application_exists(app_name)
                if app_exists:
                    logger.info(f"Waiting for ArgoCD application {app_name} to be deleted via GitOps")
                    deletion_complete = await argo_connector.wait_for_application_deletion(app_name, max_retries=20)

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
                    else:
                        deletion_results["operations"].append(
                            {
                                "type": "argocd_app_gitops_deletion",
                                "target": app_name,
                                "cluster": cluster,
                                "deployment": deployment_name,
                                "status": "timeout",
                                "error": "Application deletion via GitOps timed out after 5 retries",
                            }
                        )
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

            except Exception as e:
                deletion_results["operations"].append(
                    {
                        "type": "argocd_app_gitops_deletion",
                        "target": generate_argocd_application_name(project_name, deployment_name),
                        "cluster": cluster,
                        "deployment": deployment_name,
                        "status": "error",
                        "error": str(e),
                    }
                )
                logger.exception("Error monitoring ArgoCD application deletion")

            # Step 5: Delete Kubernetes namespace
            try:
                base_namespace = deployment.get("namespace", project_name)
                namespace = get_prefixed_namespace(cluster, base_namespace)

                namespace_used_by_others = any(
                    other_dep.get("name") != deployment_name
                    and other_dep.get("cluster") == cluster
                    and other_dep.get("namespace") == base_namespace
                    for other_dep in project_data.get("deployments", [])
                )

                if not namespace_used_by_others:
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

            # Step 5.5: Delete infrastructure if this was the last deployment using namespace-specific PostgreSQL
            try:
                from opi.core.cluster_config import get_infrastructure_namespace
                from opi.utils.naming import (
                    generate_infrastructure_application_name,
                    generate_infrastructure_manifest_path,
                )

                # Check if project uses namespace-specific PostgreSQL
                db_manager = await self.project_manager._ensure_database_manager()
                uses_namespace_db = db_manager._project_uses_namespace_postgresql(project_data)

                if uses_namespace_db:
                    # Check if any other deployments in this project still exist
                    remaining_deployments = [
                        d for d in project_data.get("deployments", []) if d.get("name") != deployment_name
                    ]

                    if not remaining_deployments:
                        # This is the LAST deployment - delete infrastructure
                        logger.info(
                            f"Last deployment deleted - cleaning up infrastructure for project '{project_name}'"
                        )

                        # 5.5.1: Delete infrastructure ArgoCD application file from GitOps
                        infra_app_name = generate_infrastructure_application_name(project_name)
                        infra_app_file_path = generate_gitops_argocd_application_path(
                            cluster, infra_app_name, ""
                        )
                        logger.info(f"Deleting infrastructure ArgoCD application file: {infra_app_file_path}")

                        gitops_connector = await self.project_manager.get_git_connector_for_argocd()
                        await gitops_connector.ensure_repo_cloned()
                        infra_file_full_path = os.path.join(
                            await gitops_connector.get_working_dir(), infra_app_file_path
                        )

                        if os.path.exists(infra_file_full_path):
                            os.remove(infra_file_full_path)
                            deletion_results["operations"].append(
                                {
                                    "type": "infrastructure_argocd_app_deletion",
                                    "target": infra_app_file_path,
                                    "status": "success",
                                }
                            )
                            logger.info(f"Deleted infrastructure ArgoCD application file: {infra_app_file_path}")

                            # 5.5.2: Rebuild kustomization and commit
                            working_dir = await gitops_connector.get_working_dir()
                            project_dir = os.path.join(working_dir, cluster, project_name)

                            kustomization_success = (
                                self.project_manager._manifest_generator.create_kustomization_files(
                                    output_dir=project_dir,
                                    namespace=get_argo_namespace(cluster),
                                )
                            )

                            if kustomization_success:
                                deletion_results["operations"].append(
                                    {
                                        "type": "infrastructure_kustomization_rebuild",
                                        "target": project_dir,
                                        "status": "success",
                                    }
                                )

                            commit_message = (
                                f"Delete infrastructure ArgoCD application for project '{project_name}'"
                            )
                            await gitops_connector.commit_and_push(commit_message)
                            deletion_results["operations"].append(
                                {"type": "infrastructure_gitops_commit", "status": "success", "message": commit_message}
                            )
                        else:
                            deletion_results["operations"].append(
                                {
                                    "type": "infrastructure_argocd_app_deletion",
                                    "target": infra_app_file_path,
                                    "status": "not_found",
                                }
                            )

                        # 5.5.3: Refresh user-applications
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

                        # 5.5.4: Wait for infrastructure Application deletion
                        infra_app_exists = await argo_connector.application_exists(infra_app_name)
                        if infra_app_exists:
                            logger.info(f"Waiting for infrastructure Application {infra_app_name} to be deleted")
                            deletion_complete = await argo_connector.wait_for_application_deletion(
                                infra_app_name, max_retries=20
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
                            else:
                                deletion_results["operations"].append(
                                    {
                                        "type": "infrastructure_app_deletion_wait",
                                        "target": infra_app_name,
                                        "status": "timeout",
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

                        # 5.5.5: Delete infrastructure namespace
                        infra_namespace = get_infrastructure_namespace(cluster, project_name)
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

                        # 5.5.6: Delete infrastructure manifests folder from deployment git repo
                        repositories = project_data.get("repositories", [])
                        if repositories:
                            main_repo = repositories[0]  # Infrastructure uses same repo as first deployment
                            repo_config = main_repo
                            manifest_connector = await self.project_manager.get_git_connector_for_deployment(
                                "infrastructure", repo_config
                            )

                            repo_path = repo_config.get("path", "")
                            infra_manifest_path = generate_infrastructure_manifest_path(cluster, project_name, repo_path)
                            logger.info(f"Deleting infrastructure manifests folder: {infra_manifest_path}")

                            await manifest_connector.ensure_repo_cloned()
                            infra_folder_full_path = os.path.join(
                                await manifest_connector.get_working_dir(), infra_manifest_path
                            )

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

                                # 5.5.7: Commit manifest deletion
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
                    else:
                        logger.info(
                            f"Skipping infrastructure deletion - {len(remaining_deployments)} deployment(s) still exist"
                        )
                else:
                    logger.debug(f"Project '{project_name}' does not use namespace-specific PostgreSQL - skipping infrastructure deletion")

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
            db_manager = await self.project_manager._ensure_database_manager()
            database_results = await db_manager.delete_resources_for_deployment(project_data, deployment)
            deletion_results["service_results"]["database"] = database_results
            deletion_results["operations"].extend(database_results["operations"])
            if database_results["errors"]:
                deletion_results["errors"].extend(database_results["errors"])

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

            # Update success status
            deletion_results["success"] = len(deletion_results["errors"]) == 0

            logger.info(
                f"Deployment deletion completed for {project_name}/{deployment_name} - Success: {deletion_results['success']}"
            )
            return deletion_results

        except HTTPException:
            raise
        except Exception as e:
            deletion_results["success"] = False
            deletion_results["errors"].append(f"Critical error during deployment deletion: {e}")
            logger.exception(f"Critical error during deployment deletion for {project_name}/{deployment_name}")
            raise HTTPException(status_code=500, detail=f"Critical error during deployment deletion: {e!s}")
