"""
The project manager handles project files. It can read, update, delete, or process them.
Processing means it can create, update, or delete any resources defined in a project file.
"""

import logging
import os
import shutil
from typing import Any, TypeVar, cast
from warnings import deprecated

from fastapi import HTTPException
from jsonpath_ng.ext import parse as jsonpath_parse
from ruamel.yaml import YAML
from ruamel.yaml.scalarstring import LiteralScalarString

from opi.connectors import create_argo_connector, create_keycloak_connector
from opi.connectors.git import (
    GitConnector,
    create_git_connector_for_argocd,
    create_git_connector_for_project_files,
    create_git_connector_from_repo_config,
    create_git_repository,
)
from opi.connectors.kubectl import KubectlConnector
from opi.core.cluster_config import (
    get_argo_namespace,
    get_database_server,
    get_ingress_ip_whitelist,
    get_ingress_postfix,
    get_ingress_tls_enabled,
    get_keycloak_discovery_url,
    get_minio_server,
    get_prefixed_namespace,
    get_storage_access_modes,
    get_storage_class_name,
)
from opi.core.config import settings
from opi.core.task_manager import TaskProgressManager
from opi.generation.manifests import ManifestGenerator
from opi.handlers.project_file_handler import ProjectFileHandler
from opi.handlers.sops import SopsHandler
from opi.services import ServiceAdapter, ServiceType
from opi.services.project_service import get_project_service
from opi.utils.age import (
    decrypt_age_content,
    decrypt_password_smart,
    decrypt_password_smart_auto,
    encrypt_age_content,
    get_decoded_project_private_key,
    get_project_public_key,
)

# Environment variables are now generated using service definitions
from opi.utils.naming import (
    generate_argocd_application_name,
    generate_argocd_appproject_prefix,
    generate_deployment_manifest_path,
    generate_gitops_argocd_application_path,
    generate_ingress_map,
    generate_manifest_name,
    generate_public_url,
    generate_pvc_name,
    generate_storage_name,
    generate_unique_name,
    get_output_filename_from_template,
)
from opi.utils.secrets import BaseSecret, DatabaseSecret, KeycloakSecret, MinIOSecret, UserSecret

# TypeVar for generic secret types
T = TypeVar("T", bound=BaseSecret)
from opi.utils.sops import encrypt_to_sops_files
from opi.utils.yaml_util import find_value_by_jsonpath, load_yaml_from_path, save_yaml_to_path, update_value_by_jsonpath

logger = logging.getLogger(__name__)


class ProjectManager:
    """Manager for project resources and deployments."""

    def __init__(
        self,
        *,
        project_file_relative_path: str | None = None,
        git_connector_for_project_files: GitConnector | None = None,
    ) -> None:
        self.__has_contents = False
        logger.debug("Initializing ProjectManager")
        self._project_file_relative_path = project_file_relative_path
        self._kubectl_connector = KubectlConnector()
        self._sops_handler = SopsHandler(self._kubectl_connector)
        self._manifest_generator = ManifestGenerator()
        self._project_file_handler = ProjectFileHandler()
        self.__git_connector_for_project_files = git_connector_for_project_files
        self.__git_connector_for_argocd = None
        # each deployment has a repository, referenced by name
        self.__git_connectors_for_deployments: dict[str, GitConnector] = {}
        # Progress manager for tracking operation status
        self.__progress_manager = None
        # Private map for storing secrets that need to be created
        # Structure: {deployment_name: {secret_type: secret_instance}}
        # Example: {"dev": {"database": DatabaseSecret(...), "keycloak": KeycloakSecret(...)}}
        self._secrets_to_create: dict[str, dict[str, BaseSecret]] = {}

        # Private map for storing environment variables that need to be tracked
        # Structure: {deployment_name: {env_key: env_vars}}
        # Example: {"dev": {"env_vars_web_storage": {"DATA_PATH": "/data"}, "env_vars_api_user": {"API_KEY": "value"}}}
        self._env_vars: dict[str, dict[str, dict[str, Any]]] = {}

        # Service managers for handling service-specific operations
        # Import here to avoid circular dependencies
        # TODO: fix me, we don't want this
        from opi.core.database_pools import get_database_pool
        from opi.manager.database_manager import DatabaseManager
        from opi.manager.keycloak_manager import KeycloakManager
        from opi.manager.minio_manager import MinioManager

        # Get the main database pool and inject it into DatabaseManager
        main_db_pool = get_database_pool("main")
        self._database_manager = DatabaseManager(self, main_db_pool)
        self._minio_manager = MinioManager(self)
        self._keycloak_manager = KeycloakManager(self)

    async def __aenter__(self) -> "ProjectManager":
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: Any) -> None:
        await self.close()

    async def get_name(self) -> str:
        contents = await self.get_contents()
        return contents["name"]

    async def get_working_dir(self) -> str:
        return await (await self.get_git_connector_for_project_files()).get_working_dir()

    async def get_git_connector_for_project_files(self) -> GitConnector:
        if self.__git_connector_for_project_files is None:
            self.__git_connector_for_project_files = await create_git_connector_for_project_files("")
            await self.__git_connector_for_project_files.ensure_repo_cloned()
        return self.__git_connector_for_project_files

    async def set_git_connector_for_project_files(self, git_connector: GitConnector) -> None:
        if self.__git_connector_for_project_files:
            raise Exception("git_connector_for_projectfiles already set")
        self.__git_connector_for_project_files = git_connector

    async def close_git_connector_for_project_files(self) -> None:
        if self.__git_connector_for_project_files:
            await self.__git_connector_for_project_files.close()
            self.__git_connector_for_project_files = None

    def _add_secret_to_create(self, deployment_name: str, secret_type: str, secret_data: BaseSecret) -> None:
        """
        Add a secret to the private secrets map for later creation.

        Args:
            deployment_name: Name of the deployment
            secret_type: Type of secret (e.g., "database", "keycloak", "vault")
            secret_data: Secret instance (BaseSecret subclass) to store
        """
        if deployment_name not in self._secrets_to_create:
            self._secrets_to_create[deployment_name] = {}
        self._secrets_to_create[deployment_name][secret_type] = secret_data
        logger.debug(f"Added {secret_type} secret for deployment {deployment_name} to secrets map")

    def _get_secret_from_map(
        self, deployment_name: str, secret_type: str, secret_class: type[T] | None = None
    ) -> T | None:
        """
        Get a secret from the private secrets map with type safety.

        Args:
            deployment_name: Name of the deployment
            secret_type: Type of secret (e.g., "database", "keycloak", "vault")
            secret_class: Expected secret class type for type safety (optional)

        Returns:
            Secret instance of the specified type if found, None otherwise
        """
        secret = self._secrets_to_create.get(deployment_name, {}).get(secret_type)
        if secret is None:
            return None

        # If secret_class is provided, verify the type for runtime safety
        if secret_class is not None and not isinstance(secret, secret_class):
            raise ValueError(
                f"Secret type mismatch for {deployment_name}.{secret_type}: "
                f"expected {secret_class.__name__}, got {type(secret).__name__}"
            )

        return cast(T, secret)

    def _register_env_var(
        self, deployment_name: str, component_name: str, service_type: str, env_vars: dict[str, Any]
    ) -> None:
        """
        Add environment variables to the private env vars map for later configuration tracking.

        Args:
            deployment_name: Name of the deployment
            component_name: Name of the component
            service_type: Type of service generating the env vars (e.g., "storage", "publish_on_web", "user")
            env_vars: Environment variables to store
        """
        if deployment_name not in self._env_vars:
            self._env_vars[deployment_name] = {}

        # Store env vars in dedicated env vars tracking map
        env_key = f"env_vars_{component_name}_{service_type}"
        self._env_vars[deployment_name][env_key] = env_vars
        logger.debug(
            f"Added {len(env_vars)} {service_type} env vars for {component_name} in deployment {deployment_name}"
        )

    def _get_env_vars_for_deployment(self, deployment_name: str) -> dict[str, Any]:
        """
        Get all environment variables for a deployment.

        Args:
            deployment_name: Name of the deployment

        Returns:
            Combined dictionary of environment variables excluding user env vars
        """
        all_env_vars = self._env_vars.get(deployment_name, {})
        # Filter out user environment variables
        filtered_env_vars = {key: value for key, value in all_env_vars.items() if not key.endswith("_user")}
        return filtered_env_vars

    def _get_project_keycloak_config_for_cluster(
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

        project_name = project_data.get("name")
        expected_realm = generate_project_realm_name(project_name, cluster)

        for entry in keycloak_list:
            if entry.get("realm") == expected_realm:
                return entry

        return None

    def _get_keycloak_url_for_cluster(self, cluster: str) -> str:
        """
        Get Keycloak URL for cluster from cluster configuration.

        Args:
            cluster: Name of the cluster

        Returns:
            Base Keycloak URL (e.g., "https://keycloak.apps.digilab.network")
        """
        from opi.core.cluster_config import get_keycloak_discovery_url

        discovery_url = get_keycloak_discovery_url(cluster)

        # Extract base URL from discovery URL
        if "/.well-known" in discovery_url:
            base_url = discovery_url.split("/.well-known")[0]
            # Remove /realms/xxx part to get just the host
            if "/realms/" in base_url:
                base_url = base_url.split("/realms/")[0]
            return base_url

        return discovery_url

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

    def _generate_storage_env_vars_from_services(self, storage_configs: list[dict[str, Any]]) -> dict[str, str]:
        """
        Generate storage environment variables using service definitions.

        Args:
            storage_configs: List of processed storage configurations

        Returns:
            Dictionary of environment variables based on service definitions
        """
        env_vars = {}

        for storage in storage_configs:
            mount_path = storage.get("mount-path")
            storage_type = storage.get("type", "persistent")

            if not mount_path:
                continue

            # Determine service type based on storage type
            if storage_type == "persistent":
                service_type = ServiceType.PERSISTENT_STORAGE
            elif storage_type == "ephemeral":
                service_type = ServiceType.TEMP_STORAGE
            else:
                raise ValueError("Unkown storage type: {storage_type}")

            # Generate env vars using service variable definitions
            for var_def in ServiceAdapter.get_service_definition(service_type).variables:
                if var_def.source == "direct":
                    # For storage services, the value is the mount path
                    env_vars[var_def.name] = mount_path
                    logger.debug(f"Generated storage env var: {var_def.name}={mount_path}")

        return env_vars

    def _generate_web_env_vars_from_services(self, hostname: str) -> dict[str, str]:
        """
        Generate web environment variables using service definitions.

        Args:
            hostname: The hostname for the component

        Returns:
            Dictionary of environment variables based on service definitions
        """
        env_vars = {}

        # Generate env vars using service variable definitions
        for var_def in ServiceAdapter.get_service_definition(ServiceType.PUBLISH_ON_WEB).variables:
            if var_def.source == "direct" and var_def.name == "PUBLIC_HOST":
                public_url = generate_public_url(hostname)
                env_vars[var_def.name] = public_url
                logger.debug(f"Generated web env var: {var_def.name}={public_url}")

        return env_vars

    def _normalize_secret_keys(self, secret_pairs: dict[str, str]) -> dict[str, str]:
        """
        Normalize secret keys to use main keys from VariableDefinition instead of aliases.
        """
        from opi.services.services import ServiceAdapter

        normalized = {}

        # Build reverse mapping from all possible keys (main + aliases) to main key
        key_mapping = {}
        for service_type in ServiceAdapter.get_all_services():
            for var_def in ServiceAdapter.get_service_definition(service_type).variables:
                main_key = var_def.name
                # Map main key to itself
                key_mapping[main_key] = main_key
                # Map all aliases to main key
                for alias in var_def.aliases:
                    key_mapping[alias] = main_key

        # Normalize the secret pairs
        for key, value in secret_pairs.items():
            main_key = key_mapping.get(key, key)  # Use original key if no mapping found
            normalized[main_key] = value

        return normalized

    async def _save_encrypted_configs_to_project_file(self) -> None:
        """
        Save encrypted deployment configurations to deployment blocks in the project file.
        Includes all secrets and environment variables for each deployment.
        """
        try:
            # Read current project data
            project_data = await self.get_contents()
            deployments = project_data.get("deployments", [])

            # Get project public key using existing utility function
            public_key = get_project_public_key(project_data)

            if not public_key:
                logger.warning("No project public key found - cannot encrypt deployment configs")
                return

            # Track if we made any changes to save
            changes_made = False

            # Update each deployment with all available secrets and env vars
            for deployment in deployments:
                deployment_name = deployment.get("name")

                # Build config dict for this deployment
                config = {"variables": {}}

                # Include secrets from _secrets_to_create if available
                if deployment_name in self._secrets_to_create:
                    for secret_type, secret_data in self._secrets_to_create[deployment_name].items():
                        if hasattr(secret_data, "to_config_data"):
                            # Handle typed secret objects using config method (main keys only, no aliases)
                            config_vars = secret_data.to_config_data()
                            config["variables"].update(config_vars)
                        elif isinstance(secret_data, dict):
                            # Handle plain dictionary secrets (same pattern as config hash generation)
                            normalized_vars = self._normalize_secret_keys(secret_data)
                            config["variables"].update(normalized_vars)

                # Include environment variables from tracking map (excluding user env vars)
                deployment_env_vars = self._get_env_vars_for_deployment(deployment_name)
                if deployment_env_vars:
                    normalized_env_vars = self._normalize_secret_keys(deployment_env_vars)
                    config["variables"].update(normalized_env_vars)

                if config["variables"]:
                    # Convert to YAML string using yaml_util
                    from opi.utils.yaml_util import dump_yaml_to_string

                    yaml_content = dump_yaml_to_string(config)

                    # Encrypt the config YAML
                    encrypted_content = await encrypt_age_content(yaml_content, public_key)
                    deployment["configuration"] = LiteralScalarString(encrypted_content)
                    changes_made = True
                    logger.debug(f"Added encrypted configuration to deployment: {deployment_name}")

            # Save back to project file using existing method
            if changes_made:
                await self.save_project_data()
                logger.info("Saved encrypted deployment configurations to project file")
            else:
                logger.debug("No configuration variables to save")

        except Exception as e:
            logger.error(f"Failed to save encrypted configs: {e}")

    async def _get_project_data_with_decrypted_configs(self) -> dict[str, Any]:
        """
        Get project data with decrypted deployment configurations for display purposes.

        Returns:
            Project data dictionary with decrypted configuration variables
        """
        project_data = await self.get_contents()
        deployments = project_data.get("deployments", [])

        # Get project private key for decryption
        private_key = None
        try:
            private_key = await get_decoded_project_private_key(project_data)
        except Exception as e:
            logger.warning(f"Could not get project private key for config decryption: {e}")
            return project_data

        if not private_key:
            return project_data

        # Process each deployment to decrypt its configuration
        processed_deployments = []
        for deployment in deployments:
            deployment_copy = deployment.copy()

            if "configuration" in deployment:
                try:
                    from opi.utils.age import decrypt_age_content
                    from opi.utils.yaml_util import load_yaml_from_string

                    # Decrypt the configuration
                    decrypted_yaml = await decrypt_age_content(deployment["configuration"], private_key)

                    # Parse the YAML using yaml_util
                    config_data = load_yaml_from_string(decrypted_yaml)

                    deployment_copy["decrypted_configuration"] = config_data
                    logger.debug(f"Decrypted configuration for deployment: {deployment.get('name')}")

                except Exception as e:
                    logger.warning(f"Failed to decrypt configuration for deployment {deployment.get('name')}: {e}")
                    deployment_copy["decrypted_configuration"] = None
            else:
                deployment_copy["decrypted_configuration"] = None

            processed_deployments.append(deployment_copy)

        # Update project data with processed deployments
        project_data_copy = project_data.copy()
        project_data_copy["deployments"] = processed_deployments

        return project_data_copy

    async def get_git_connector_for_argocd(self) -> GitConnector:
        if self.__git_connector_for_argocd is None:
            self.__git_connector_for_argocd = await create_git_connector_for_argocd(await self.get_name())
            await self.__git_connector_for_argocd.ensure_repo_cloned()
        return self.__git_connector_for_argocd

    async def set_git_connector_for_argocd(self, git_connector: GitConnector) -> None:
        if self.__git_connector_for_argocd:
            raise Exception("git_connector_for_argocd already set")
        self.__git_connector_for_argocd = git_connector

    async def close_git_connector_for_argocd(self) -> None:
        if self.__git_connector_for_argocd:
            await self.__git_connector_for_argocd.close()
            self.__git_connector_for_argocd = None

    def set_progress_manager(self, task_progress_manager: "TaskProgressManager") -> None:
        """Set the task progress manager for tracking operation status."""
        self.__progress_manager = task_progress_manager

    def get_progress_manager(self) -> "TaskProgressManager | None":
        """Get the task progress manager for tracking operation status."""
        return self.__progress_manager

    async def set_git_connector_for_deployment(self, name: str, git_connector: GitConnector) -> None:
        if name in self.__git_connectors_for_deployments:
            raise Exception(f"git_connector_for_deployments already set for {name}")
        self.__git_connectors_for_deployments[name] = git_connector

    async def get_git_connector_for_deployment(self, name: str, repo_config: dict[str, str]) -> GitConnector:
        if name not in self.__git_connectors_for_deployments:
            if "project_name" not in repo_config:
                repo_config["project_name"] = await self.get_name()
            self.__git_connectors_for_deployments[name] = await create_git_connector_from_repo_config(repo_config)
        return self.__git_connectors_for_deployments[name]

    async def close_git_connectors_for_deployments(self) -> None:
        for name in self.__git_connectors_for_deployments:
            await self.__git_connectors_for_deployments[name].close()
        self.__git_connectors_for_deployments = {}

    # TODO: we may want to process a file anyway
    async def has_deployments_for_current_cluster(self) -> bool:
        project_data = await self.get_contents()
        project_name = project_data["name"]

        # Check if deployments exist and have cluster configurations
        deployments = project_data.get("deployments", [])
        if not deployments:
            logger.debug(f"Project '{project_name}' has no deployments, skipping cluster validation")
            return True

        # Get the configured cluster manager
        configured_cluster = settings.CLUSTER_MANAGER
        logger.debug(f"Configured cluster manager: {configured_cluster}")

        # Filter deployments to only include those targeting this cluster
        matching_deployments = []
        skipped_deployments = []

        for deployment in deployments:
            deployment_name = deployment.get("name", "unknown")
            target_cluster = deployment.get("cluster")

            if target_cluster == configured_cluster:
                matching_deployments.append(deployment)
            else:
                skipped_deployments.append(deployment_name)
                logger.debug(
                    f"Project '{project_name}' deployment '{deployment_name}' targets cluster "
                    f"'{target_cluster}' but CLUSTER_MANAGER is '{configured_cluster}' - skipping"
                )

        # Update the project data with filtered deployments
        project_data["deployments"] = matching_deployments

        if skipped_deployments:
            logger.info(
                f"Project '{project_name}' has {len(skipped_deployments)} deployment(s) "
                f"for other clusters: {', '.join(skipped_deployments)}"
            )

        if not matching_deployments:
            logger.info(
                f"Project '{project_name}' has no deployments for cluster '{configured_cluster}' - skipping processing"
            )
            return False

        logger.debug(
            f"Project '{project_name}' cluster validation passed with {len(matching_deployments)} "
            f"deployment(s) for cluster '{configured_cluster}'"
        )
        return True

    async def create_project_repository(self, project_data: dict[str, Any]) -> bool:
        """
        Create a Git repository for the project.

        Args:
            project_data: The parsed project data

        Returns:
            True if the repository was created successfully, False otherwise
        """
        project_name = project_data.get("name")
        logger.debug(f"Creating repository for project: {project_name}")

        try:
            # Get the repository URL from the project data
            repositories = project_data.get("repositories", [])
            if not repositories:
                logger.error("No repositories defined in project data")
                return False

            main_repo = repositories[0]  # Use the first repository as the main repo
            repo_url = main_repo.get("url")

            # Extract repository name from the URL path instead of using the 'name' field
            if repo_url:
                # Extract repo name from URL (e.g., "/srv/git/example-project-infra.git" -> "example-project-infra")
                repo_name = os.path.basename(repo_url)
                if repo_name.endswith(".git"):
                    repo_name = repo_name[:-4]  # Remove .git extension
            else:
                logger.error(f"No URL defined for repository: {main_repo.get('name', 'unknown')}")
                return False

            # Create the repository
            result = await create_git_repository(
                server_host=settings.GIT_SERVER_HOST,
                repo_name=repo_name,
                ssh_key_path=settings.GIT_SERVER_KEY_PATH,
                ssh_port=settings.GIT_SERVER_PORT,
                ssh_user=settings.GIT_SERVER_USER,
            )

            if result:
                logger.info(f"Successfully created repository: {repo_name}")
            else:
                logger.error(f"Failed to create repository: {repo_name}")

            return result
        except Exception:
            logger.exception("Error creating project repository")
            return False

    async def get_project_full_file_path(self):
        if self._project_file_relative_path is None:
            raise ValueError("Project file relative path is not set")
        git_connector_for_project_files = await self.get_git_connector_for_project_files()
        git_working_dir = await git_connector_for_project_files.get_working_dir()
        return os.path.join(git_working_dir, str(self._project_file_relative_path))

    async def save_project_data(self) -> None:
        project_full_file_path = await self.get_project_full_file_path()
        yaml = YAML()
        yaml.default_flow_style = False
        yaml.preserve_quotes = True
        yaml.width = 4096

        with open(project_full_file_path, "w") as f:
            yaml.dump(await self.get_contents(), f)

    async def check_and_create_namespaces(self, deployment_name: str | None = None) -> bool:
        """
        Check and create namespaces for all deployments in the project for this cluster.

        Args:
            deployment_name: Optional deployment name to process only specific deployment

        Returns:
            True if all namespaces were checked/created successfully
        """

        await self.get_project_full_file_path()

        project_data: dict[str, str | list | dict[str, str]] = await self.get_contents()
        logger.info(f"Checking namespaces for project: {project_data['name']}")

        # Track namespace creation with progress manager if available
        progress_manager = self.get_progress_manager()
        namespace_subtask = None
        if progress_manager:
            namespace_subtask = progress_manager.add_task("Kubernetes namespace(s) aanmaken")
        # TODO: make classes for the project so we can use structured typing better
        deployments = cast(list[dict[str, str | list | dict[str, str]]], project_data.get("deployments", []))

        # Filter deployments if specific deployment_name is provided
        if deployment_name:
            deployments = [d for d in deployments if d.get("name") == deployment_name]
            logger.info(f"Checking namespaces only for deployment: {deployment_name}")

        if not deployments:
            logger.info(f"No deployments found in project {project_data['name']}")
            return True

        all_successful = True

        for deployment in (d for d in deployments if d.get("cluster") == settings.CLUSTER_MANAGER):
            namespace = get_prefixed_namespace(settings.CLUSTER_MANAGER, cast(str, deployment["namespace"]))

            logger.info(
                f"Checking namespace '{namespace}' for deployment '{deployment['name']}' for project '{project_data['name']}':"
            )
            # Check if namespace exists
            namespace_exists = await self._kubectl_connector.namespace_exists(namespace)
            if namespace_exists:
                logger.info(
                    f"Namespace '{namespace}' already exists for deployment '{deployment['name']}' for project '{project_data['name']}'"
                )
                # Set namespace for monitoring even if it already exists
                if progress_manager:
                    progress_manager.set_namespace(namespace)
                continue

            logger.info(
                f"Creating namespace '{namespace}' for deployment '{deployment['name']}' for project '{project_data['name']}':"
            )

            # Create the namespace using the manifest template
            manifest_path = os.path.join(settings.MANIFESTS_PATH, "namespace.yaml.jinja")

            # Template variables
            variables = {"namespace": namespace, "manager": get_argo_namespace(settings.CLUSTER_MANAGER)}

            await self._kubectl_connector.apply_manifest(manifest_path, variables)

            # Apply the argocd.argoproj.io/managed-by label after creating the namespace
            manager_value = get_argo_namespace(settings.CLUSTER_MANAGER)
            await self._kubectl_connector.apply_label_to_resource(
                resource_type="namespace",
                resource_name=namespace,
                label_key="argocd.argoproj.io/managed-by",
                label_value=manager_value,
            )

            if progress_manager:
                progress_manager.set_namespace(namespace)

        # Complete namespace subtask if progress manager is available
        if progress_manager and namespace_subtask:
            if all_successful:
                progress_manager.complete_task(namespace_subtask)
            else:
                progress_manager.fail_task(namespace_subtask, "Failed to create one or more namespaces")

        return all_successful

    async def check_and_create_sops_secrets_in_namespaces(self, deployment_name: str | None = None) -> None:
        """
        Creates SOPS secrets in the specified namespaces. If no SOPS information is in the project file,
        a new sops pair is created.

        Args:
            deployment_name: Optional deployment name to process only specific deployment
        """
        contents = await self.get_contents()
        project_name = contents.get("name")

        deployments = contents.get("deployments", [])

        # Filter deployments if specific deployment_name is provided
        if deployment_name:
            deployments = [d for d in deployments if d.get("name") == deployment_name]
            logger.info(f"Creating SOPS secrets only for deployment: {deployment_name}")

        if not deployments:
            logger.warning("No deployments found in project: {project_name}")
            return

        public_key = get_project_public_key(contents)
        private_key = await get_decoded_project_private_key(contents)
        # age_keys_created = False

        # Try to get project SOPS keys upfront (prioritizing reuse over generation)
        # TODO: we may only need to run this code (once) if a namespace does not have a sops secret
        #  so this should become a separate function
        # if not public_key and not encoded_private_key:
        #     logger.info(f"Project {project_name} has no SOPS information in project data, so we create a new sops pair")
        #     private_key, encoded_private_key, public_key = await generate_and_encrypt_sops_key_pair()
        #     contents["config"]["age-public-key"] = public_key
        #     contents["config"]["age-private-key"] = LiteralScalarString(encoded_private_key)
        #     age_keys_created = True
        #     # TODO: we should only call the save and commit and push at the end of project processing, but for now we call it here
        #     await self.save_project_data()
        #     await (await self.get_git_connector_for_project_files()).commit_and_push("Added sops keys to project data")
        # else:
        #     logger.info(f"Project {project_name} contains SOPS information in project data")
        #     private_key = await decrypt_age_content(encoded_private_key, cast(str, settings.SOPS_AGE_PRIVATE_KEY))

        # TODO: rethink logic for checking the cluster_manager all the time
        for deployment in (d for d in deployments if d.get("cluster") == settings.CLUSTER_MANAGER):
            cluster_name = deployment["cluster"]
            base_namespace = deployment["namespace"]
            namespace = get_prefixed_namespace(cluster_name, base_namespace)
            logger.info(f"Checking SOPS secret for project {project_name} in namespace {namespace}")

            existing_secret = await self._kubectl_connector.get_sops_secret_from_namespace(namespace)

            create_sops_secret = False
            if existing_secret is None:
                logger.info(f"SOPS secret not found for project {project_name} in namespace {namespace}")
                create_sops_secret = True
            elif existing_secret is not None and public_key not in existing_secret:
                create_sops_secret = True
                logger.warning(
                    f"Found existing SOPS secret in namespace {namespace} for project {project_name}. "
                    f"Project has new SOPS keys - the old secret is now obsolete and will be replaced. "
                    f"Existing database/MinIO/Keycloak credentials will be preserved from their respective secrets."
                )
                # Delete the old SOPS secret first to ensure clean replacement
                try:
                    await self._kubectl_connector.delete_resource("secret", "sops-age-key", namespace)
                    logger.info(f"Deleted old SOPS secret from namespace {namespace}")
                except Exception as e:
                    logger.warning(f"Failed to delete old SOPS secret (continuing anyway): {e}")

            if create_sops_secret:
                await self._sops_handler.store_project_sops_key_in_namespace(namespace, private_key, public_key)
                logger.info(f"Created new SOPS secret for project {project_name} in namespace {namespace}")
            else:
                logger.info(f"Found existing SOPS secret for project {project_name} in namespace {namespace}")

    async def _create_argocd_application(self, deployment_name: str | None = None) -> bool:
        project_data = await self.get_contents()
        project_name = project_data["name"]
        logger.debug(f"Creating ArgoCD application for project: {project_name}")

        git_connector_for_argocd = await self.get_git_connector_for_argocd()
        working_dir = await git_connector_for_argocd.get_working_dir()

        try:
            # Create an ArgoCD application for each deployment
            deployments = project_data.get("deployments", [])

            # Filter deployments if specific deployment_name is provided
            if deployment_name:
                deployments = [d for d in deployments if d.get("name") == deployment_name]
                logger.info(f"Creating ArgoCD application only for deployment: {deployment_name}")

            if not deployments:
                logger.error("No deployments defined in project data")
                return False

            all_succeeded = True

            for deployment in deployments:
                cluster_name = deployment.get("cluster")
                base_namespace = deployment.get("namespace")
                namespace = get_prefixed_namespace(cluster_name, base_namespace)

                # Get repository information
                repo_name = deployment.get("repository")
                repo_info = next((r for r in project_data.get("repositories", []) if r.get("name") == repo_name), None)

                if not repo_info:
                    logger.error(f"Repository not found: {repo_name}")
                    all_succeeded = False
                    continue

                # ArgoCD application name
                app_name = generate_argocd_application_name(project_name, deployment["name"])

                # Combine repository path, cluster name, project name, and deployment name
                cluster_name = deployment.get("cluster", "local")
                repo_path = repo_info.get("path", "")
                if repo_path:
                    deployment_path = f"{repo_path}/{cluster_name}/{project_name}/{deployment['name']}"
                else:
                    deployment_path = f"{cluster_name}/{project_name}/{deployment['name']}"

                # Create ArgoCD application manifest content
                argocd_app_content = self._generate_argocd_app_manifest(
                    name=app_name,
                    namespace=get_argo_namespace(cluster_name),
                    argo_project=generate_argocd_appproject_prefix(project_name, base_namespace),
                    repo_url=repo_info.get("url"),
                    target_revision=repo_info.get("branch", "main"),
                    repo_path=deployment_path,
                    destination_namespace=namespace,
                    project_label=project_name,
                )

                # Write the ArgoCD application manifest to the checked out repository
                template_filename = "argocd-application.yaml.jinja"
                output_filename = get_output_filename_from_template(template_filename, app_name)

                # Create cluster/project subdirectory structure
                cluster_name = deployment.get("cluster")
                project_dir = os.path.join(str(working_dir), str(cluster_name), str(project_name))
                os.makedirs(project_dir, exist_ok=True)

                app_file_path = os.path.join(project_dir, output_filename)

                with open(app_file_path, "w") as f:
                    f.write(argocd_app_content)

                logger.info(f"Successfully created ArgoCD application file: {app_file_path}")

            return all_succeeded
        except Exception as e:
            logger.exception(f"Error creating ArgoCD application: {e}")
            return False

    async def _create_argocd_repositories(self) -> None:
        """
        Create ArgoCD repository manifests for the project in the provided git repository.
        This creates ArgoCD repository manifest files for each repository defined in the project.
        Repository files are created with SOPS encryption using .to-sops.yaml naming convention.
        """
        project_data = await self.get_contents()
        project_name = project_data.get("name")

        git_connector_for_argocd = await self.get_git_connector_for_argocd()
        working_dir = await git_connector_for_argocd.get_working_dir()

        logger.info(f"Creating ArgoCD repositories for project: {project_name} ===")

        # Get all repositories from the project data
        repositories = project_data.get("repositories", [])
        if not repositories:
            logger.error("No repositories defined in project data")
            return

        # Get all clusters used by deployments to determine folder structure
        deployments = project_data.get("deployments", [])
        clusters_used = set()
        for deployment in deployments:
            cluster_name = deployment["cluster"]
            clusters_used.add(cluster_name)

        cluster_name = next(iter(clusters_used))
        project_dir = os.path.join(str(working_dir), str(cluster_name), str(project_name))
        logger.info(f"Creating cluster/project directory: {project_dir}")
        os.makedirs(project_dir, exist_ok=True)

        # Prepare manifest configurations for batch creation
        manifest_configs = []
        for repository in repositories:
            repo_name = repository.get("name")
            repo_url = repository.get("url")

            logger.info(f"  Processing repository: {repo_name} ({repo_url})")

            if not repo_name or not repo_url:
                logger.error(f"  Repository missing name or URL: {repository}")
                continue

            # Create unique name combining project and repository name
            unique_repo_name = f"{project_name}-{repo_name}"
            logger.info(f"  Unique repository name: {unique_repo_name}")

            # Prepare variables for the manifest template
            variables = await self._prepare_argocd_repository_variables(
                name=unique_repo_name,
                namespace=get_argo_namespace(cluster_name),
                repository=repository,
                repo_type="git",
            )

            # Determine template path based on authentication method
            repository_url = repository.get("url", "")
            is_https = repository_url.startswith("https://")
            template_filename = "argo-repository-https.yaml.jinja" if is_https else "argo-repository.yaml.jinja"
            template_path = os.path.join(settings.MANIFESTS_PATH, template_filename)
            output_filename = get_output_filename_from_template(template_filename, unique_repo_name)

            manifest_config = {
                "template_path": template_path,
                "values": variables,
                "output_filename": output_filename,
                "use_sops": True,
            }

            manifest_configs.append(manifest_config)
            logger.info(f"  Added repository manifest config for {unique_repo_name} (SOPS: True)")

        # Use manifest generator to create all repository manifests with SOPS encryption
        logger.info(f"Creating {len(manifest_configs)} ArgoCD repository manifests with SOPS encryption")
        created_files = []

        for config in manifest_configs:
            manifest_path = self._manifest_generator.create_manifest_file(
                template_path=config["template_path"],
                values=config["values"],
                output_dir=project_dir,
                output_filename=config["output_filename"],
                use_sops=config["use_sops"],
            )
            created_files.append(manifest_path)
            logger.info(f"Successfully created repository manifest: {os.path.basename(manifest_path)}")

        encrypt_to_sops_files(project_dir, cast(str, settings.SOPS_AGE_PUBLIC_KEY))

    async def _create_argocd_app_project(self) -> None:
        project_data = await self.get_contents()
        project_name = project_data["name"]
        logger.debug(f"Creating ArgoCD AppProject for project: {project_name}")

        # Get deployments and group by cluster
        deployments = project_data.get("deployments", [])
        if not deployments:
            logger.error("No deployments defined in project data")

        # Group deployments by cluster to create AppProject per cluster
        clusters_namespaces = {}
        for deployment in deployments:
            cluster_name = deployment.get("cluster")
            base_namespace = deployment.get("namespace")
            if cluster_name not in clusters_namespaces:
                clusters_namespaces[cluster_name] = set()
            clusters_namespaces[cluster_name].add(base_namespace)

        git_connector_for_argocd = await self.get_git_connector_for_argocd()
        working_dir = await git_connector_for_argocd.get_working_dir()

        # Create AppProject for each cluster and each namespace within that cluster
        for cluster_name, base_namespaces in clusters_namespaces.items():
            # Create one AppProject per namespace within this cluster
            for base_destination_namespace in base_namespaces:
                # Generate consistent project-namespace name for AppProject
                appproject_name = generate_argocd_appproject_prefix(project_name, base_destination_namespace)

                # Create ArgoCD AppProject manifest content
                appproject_content = self._generate_argocd_appproject_manifest(
                    name=appproject_name,
                    namespace=get_argo_namespace(cluster_name),
                    destination_namespace=get_prefixed_namespace(cluster_name, base_destination_namespace),
                    project_label=project_name,
                )

                # Write the AppProject manifest to the checked out repository
                template_filename = "argocd-appproject.yaml.jinja"
                output_filename = get_output_filename_from_template(template_filename, appproject_name)

                # Create cluster/project subdirectory structure
                project_dir = os.path.join(working_dir, cluster_name, project_name)
                os.makedirs(project_dir, exist_ok=True)

                # TODO: this does not feel like the right place to do this
                appproject_file_path = os.path.join(project_dir, output_filename)
                with open(appproject_file_path, "w") as f:
                    f.write(appproject_content)

                logger.info(
                    f"Successfully created ArgoCD AppProject file for cluster {cluster_name}, namespace {base_destination_namespace}: {appproject_file_path}"
                )
            return None
        return None

    def _generate_argocd_app_manifest(
        self, name, namespace, argo_project, repo_url, target_revision, repo_path, destination_namespace, project_label
    ):
        """
        Generate an ArgoCD application manifest using the template file.

        Args:
            name: Application name
            namespace: ArgoCD namespace
            argo_project: ArgoCD project
            repo_url: Git repository URL
            target_revision: Git branch or tag
            repo_path: Path in the Git repository
            destination_namespace: Target namespace
            project_label: Project label

        Returns:
            String containing the YAML manifest
        """
        # Path to the ArgoCD application manifest template
        manifest_path = os.path.join(settings.MANIFESTS_PATH, "argocd-application.yaml.jinja")

        # Prepare variables for template
        variables = {
            "name": name,
            "namespace": namespace,
            "argo_project": argo_project,
            "repoURL": repo_url,
            "targetRevision": target_revision,
            "repoPath": repo_path,
            "labels": {"project": project_label},
            "destination": {"namespace": destination_namespace},
        }

        # Read the manifest template
        try:
            with open(manifest_path) as f:
                manifest_template = f.read()

            # Process the template with the variables
            processed_manifest = self._manifest_generator.template_manifest(manifest_template, variables)
            return processed_manifest
        except Exception as e:
            logger.exception(f"Error generating ArgoCD application manifest: {e}")
            raise

    def _generate_argocd_appproject_manifest(self, name, namespace, destination_namespace, project_label):
        """
        Generate an ArgoCD AppProject manifest using the template file.

        Args:
            name: AppProject name
            namespace: ArgoCD namespace
            destination_namespace: Target namespace pattern (e.g., "project-*")
            project_label: Project label

        Returns:
            String containing the YAML manifest
        """
        # Path to the ArgoCD AppProject manifest template
        manifest_path = os.path.join(settings.MANIFESTS_PATH, "argocd-appproject.yaml.jinja")

        # Prepare variables for template
        variables = {
            "name": name,
            "namespace": namespace,
            "labels": {"project": project_label},
            "destination": {"namespace": destination_namespace, "server": "https://kubernetes.default.svc"},
        }

        # Read the manifest template
        try:
            with open(manifest_path) as f:
                manifest_template = f.read()

            # Process the template with the variables
            processed_manifest = self._manifest_generator.template_manifest(manifest_template, variables)
            return processed_manifest
        except Exception as e:
            logger.exception(f"Error generating ArgoCD AppProject manifest: {e}")
            raise

    async def _generate_argocd_repository_manifest(self, name, namespace, repository, repo_type):
        """
        Generate an ArgoCD repository manifest using the template file.
        Supports both SSH and HTTPS authentication methods.

        Args:
            name: Repository name
            namespace: ArgoCD namespace
            repository: Repository configuration dictionary
            repo_type: Repository type (e.g., "git")

        Returns:
            String containing the YAML manifest
        """
        repository_url = repository.get("url", "")
        username = repository.get("username")
        password = repository.get("password")

        # GitConnector handles URL cleaning internally
        clean_url = repository_url

        # Determine authentication method
        is_https = clean_url.startswith("https://")

        # Decrypt password if encrypted
        decrypted_password = None
        if password:
            decrypted_password = await decrypt_password_smart(password, settings.SOPS_AGE_PRIVATE_KEY)

        # Prepare variables for template
        variables = {
            "name": name,
            "namespace": namespace,
            "type": repo_type,
            "repository_url": clean_url,
            "is_https": is_https,
            "username": username or "",
            "password": decrypted_password or "",
        }

        # Choose template based on authentication method
        if is_https:
            # Use HTTPS template for all HTTPS repositories
            manifest_path = os.path.join(settings.MANIFESTS_PATH, "argo-repository-https.yaml.jinja")
            with open(manifest_path) as f:
                manifest_template = f.read()
            logger.info(
                f"Using HTTPS template for repository {name} (credentials: {'YES' if username and decrypted_password else 'NO'})"
            )
        else:
            # Use SSH template for SSH and git:// repositories
            manifest_path = os.path.join(settings.MANIFESTS_PATH, "argo-repository.yaml.jinja")
            with open(manifest_path) as f:
                manifest_template = f.read()
            logger.info(f"Using SSH template for repository {name}")

        # Read the manifest template
        try:
            # Process the template with the variables
            processed_manifest = self._manifest_generator.template_manifest(manifest_template, variables)
            return processed_manifest
        except Exception as e:
            logger.exception(f"Error generating ArgoCD repository manifest: {e}")
            raise

    async def _prepare_argocd_repository_variables(self, name, namespace, repository, repo_type):
        """
        Prepare variables for ArgoCD repository manifest templates.

        Args:
            name: Repository name
            namespace: ArgoCD namespace
            repository: Repository configuration dictionary
            repo_type: Repository type (e.g., "git")

        Returns:
            Dictionary containing variables for template substitution
        """
        repository_url = repository.get("url", "")
        username = repository.get("username")
        password = repository.get("password")

        # GitConnector handles URL cleaning internally
        clean_url = repository_url

        # Determine authentication method
        is_https = clean_url.startswith("https://")

        # Decrypt password if encrypted
        decrypted_password = None
        if password:
            decrypted_password = await decrypt_password_smart(password, settings.SOPS_AGE_PRIVATE_KEY)

        # Prepare variables for template
        return {
            "name": name,
            "namespace": namespace,
            "type": repo_type,
            "repository_url": clean_url,
            "is_https": is_https,
            "username": username or "",
            "password": decrypted_password or "",
        }

    def _analyze_deployment_changes(self, changes: dict[str, Any], current_yaml: dict[str, Any]) -> dict[str, Any]:
        """
        Analyze changes specifically in the deployments section.

        If no deployment changes are detected, return all current deployments as newly created.
        This ensures that the system processes all deployments when there are no specific changes.

        Args:
            changes: The structured changes from DeepDiff analysis
            current_yaml: Current YAML content

        Returns:
            Dictionary with deployment-specific changes: added, changed, deleted
        """
        deployment_changes = {"added": {}, "changed": {}, "deleted": {}}

        # Check for deployment-related changes
        has_deployment_changes = False

        # Look for changes in the deployments section
        for path, value in changes["added"].items():
            if path.startswith("deployments.") or path == "deployments":
                deployment_changes["added"][path] = value
                has_deployment_changes = True
                logger.debug(f"Added deployment change: {path}")

        for path, value in changes["changed"].items():
            if path.startswith("deployments."):
                deployment_changes["changed"][path] = value
                has_deployment_changes = True
                logger.debug(f"Changed deployment change: {path}")

        for path, value in changes["deleted"].items():
            if path.startswith("deployments.") or path == "deployments":
                deployment_changes["deleted"][path] = value
                has_deployment_changes = True
                logger.debug(f"Deleted deployment change: {path}")

        # If no deployment changes detected, treat all current deployments as newly created
        if not has_deployment_changes:
            logger.info("No deployment-specific changes detected - treating all deployments as newly created")
            current_deployments = current_yaml.get("deployments", [])

            if current_deployments:
                # Create a path for each deployment treating them as added
                for i, deployment in enumerate(current_deployments):
                    deployment_name = deployment.get("name", f"deployment-{i}")
                    deployment_path = f"deployments.{i}"
                    deployment_changes["added"][deployment_path] = deployment
                    logger.debug(f"Treating deployment as newly created: {deployment_name}")

                logger.info(f"Treating {len(current_deployments)} existing deployment(s) as newly created")
            else:
                logger.info("No deployments found in current project configuration")

        return deployment_changes

    async def process_project_from_git(
        self,
        relative_project_file_path: str,
        task_progress_manager: "TaskProgressManager | None" = None,
        deployment_name: str | None = None,
    ) -> bool:
        """
        Process a project file from the Git repository.

        The process follows these steps:
        0. Fetch the project file from the Git repository
        1. Create a Git repository for infrastructure manifests
        2. Add a secret file to the repository and commit/push it
        3. Create a namespace in the Kubernetes cluster
        4. Create an ArgoCD application and push it to the ArgoCD config repository

        Args:
            relative_project_file_path: Path to the project file within the Git repository
            task_progress_manager: Optional progress manager for tracking operation status
            deployment_name: Optional deployment name to process only specific deployment

        Returns:
            True if all operations were successful, False otherwise
        """

        if self._project_file_relative_path and relative_project_file_path != self._project_file_relative_path:
            raise Exception(f"Project file path already set: {self._project_file_relative_path}")

        # Set the task progress manager if provided
        if task_progress_manager:
            self.set_progress_manager(task_progress_manager)

        # Track critical failures
        critical_failures = []

        self._project_file_relative_path = relative_project_file_path

        logger.info(f"Processing project from Git: {relative_project_file_path}")

        try:
            project_full_file_path = await self.get_project_full_file_path()
            git_connector_for_project_files = await self.get_git_connector_for_project_files()

            # Use the file handler to analyze changes
            # TODO: change detection may turn out too difficult or unpredictable, so perhaps we should use API calls instead for partial changes
            analysis = await self._project_file_handler.analyze_project_changes(
                git_connector_for_project_files, project_full_file_path, relative_project_file_path
            )

            current_yaml = analysis["current_yaml"]
            previous_yaml = analysis["previous_yaml"]
            changes = analysis["changes"]

            # Log the changes summary
            if previous_yaml is None:
                logger.info("Processing new project file (no previous version found)")
            else:
                logger.info(
                    f"Detected changes - Added: {len(changes['added'])}, "
                    f"Changed: {len(changes['changed'])}, Deleted: {len(changes['deleted'])}"
                )

            # Step 1.5: Analyze deployment-specific changes
            logger.info("Step 1.5: Analyzing deployment changes")
            deployment_changes = self._analyze_deployment_changes(changes, current_yaml)

            logger.info(
                f"Deployment changes - Added: {len(deployment_changes['added'])}, "
                f"Changed: {len(deployment_changes['changed'])}, Deleted: {len(deployment_changes['deleted'])}"
            )

            # Step 2: Process the project with change context
            logger.info("Step 2: Processing project with change detection")

            # For now, still process the entire project but with change context available
            # TODO: In future iterations, we can use the changes to process only what's needed
            await self.process_project(deployment_name)

            logger.info(
                "Triggering ArgoCD sync for user-applications and project applications after project processing"
            )
            argo_connector = create_argo_connector()

            # Refresh user-applications first (contains project definitions)
            await argo_connector.refresh_application("user-applications")

            project_data = await self.get_contents()
            project_name = project_data.get("name")
            deployments = project_data.get("deployments", [])

            if deployments and project_name:
                logger.info(f"Syncing {len(deployments)} project applications for {project_name}")
                for deployment in deployments:
                    deployment_name = deployment.get("name")
                    if deployment_name:
                        app_name = generate_argocd_application_name(project_name, deployment_name)
                        try:
                            # Check if application exists before trying to sync
                            if await argo_connector.application_exists(app_name):
                                logger.info(f"Refreshing ArgoCD application: {app_name}")
                                sync_result = await argo_connector.refresh_application(app_name)
                                if sync_result:
                                    logger.info(f"Successfully refreshed application: {app_name}")
                                else:
                                    logger.warning(f"Failed to sync application: {app_name}")
                            else:
                                logger.debug(f"ArgoCD application {app_name} does not exist yet, skipping sync")
                        except Exception as e:
                            logger.warning(f"Error syncing application {app_name}: {e}")
                            # Don't fail the entire refresh if one app sync fails

            # Check for critical failures
            if critical_failures:
                logger.error(f"Project processing completed with {len(critical_failures)} critical failures:")
                for failure in critical_failures:
                    logger.error(f"  - {failure}")
                return False
            return True
        except Exception as e:
            logger.exception(f"Error processing project from Git: {e}")
            return False
        finally:
            await self.close()

    def _extract_added_changes(self, project_data: dict[str, Any]) -> dict[str, Any]:
        """
        Extract added changes from project data.
        Currently returns all data as "added" - future versions will support diffs.

        Args:
            project_data: The parsed project data

        Returns:
            Dictionary containing changes marked as "added"
        """
        logger.debug("Extracting added changes (all items marked as added)")
        return project_data  # For now, treat everything as "added"

    def _get_project_repositories(self, project_data: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Get list of repositories from project data.

        Args:
            project_data: The parsed project data

        Returns:
            List of repository configurations
        """
        repositories = project_data.get("repositories", [])
        logger.debug(f"Found {len(repositories)} repositories in project")
        return repositories

    async def _get_missing_repositories(self, repositories: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Get list of repositories that don't exist yet.

        Args:
            repositories: List of repository configurations

        Returns:
            List of repositories that need to be created
        """
        missing_repos = []
        for repo in repositories:
            repo_url = repo.get("url", "")
            # Skip GitHub repositories as they're external
            if "github.com" in repo_url:
                logger.debug(f"Skipping external repository: {repo_url}")
                continue

            # For local git server repositories, assume they need to be created
            # TODO: Add actual existence check when git server API is available
            missing_repos.append(repo)
            logger.debug(f"Repository marked as missing: {repo.get('name', 'unknown')}")

        logger.info(f"Found {len(missing_repos)} missing repositories")
        return missing_repos

    async def _create_repositories(
        self, missing_repositories: list[dict[str, Any]], project_data: dict[str, Any]
    ) -> bool:
        """
        Create missing repositories.

        Args:
            missing_repositories: List of repositories that need to be created
            project_data: The parsed project data for context

        Returns:
            True if all repositories were created successfully, False otherwise
        """
        if not missing_repositories:
            logger.debug("No repositories to create")
            return True

        logger.info(f"Creating {len(missing_repositories)} repositories")
        return await self.create_project_repository(project_data)

    async def create_argocd_resources(self, deployment_name: str | None = None) -> None:
        """
        Create all ArgoCD resources for this project.

        Args:
            deployment_name: Optional deployment name to create resources only for specific deployment
        """
        project_data = await self.get_contents()
        project_name = project_data["name"]
        logger.info(f"Creating ArgoCD resources for {project_name}")

        await self._create_argocd_repositories()
        await self._create_argocd_app_project()
        await self._create_argocd_application(deployment_name)
        await self._create_argocd_kustomization_file()
        await (await self.get_git_connector_for_argocd()).commit_and_push(
            f"Added ArgoCD resources for project {project_name}"
        )

    async def _process_application_manifests(self, deployment_name: str | None = None) -> None:
        """
        Process application manifests for all project repositories.

        Args:
            deployment_name: Optional deployment name to process only specific deployment

        Returns: None
        """
        project_data = await self.get_contents()
        project_name = project_data.get("name")
        logger.info(f"Processing application manifests for {project_name}")

        repositories = project_data.get("repositories", [])
        if not repositories:
            logger.warning("No repositories defined in project data")
            return

        # Group deployments by repository
        deployments = project_data.get("deployments", [])

        # Filter deployments if specific deployment_name is provided
        if deployment_name:
            deployments = [d for d in deployments if d.get("name") == deployment_name]
            logger.info(f"Processing application manifests only for deployment: {deployment_name}")

        deployments_by_repo = {}
        for deployment in deployments:
            repo_name = deployment.get("repository")
            if repo_name not in deployments_by_repo:
                deployments_by_repo[repo_name] = []
            deployments_by_repo[repo_name].append(deployment)

        # Process each repository
        for repo_name, repo_deployments in deployments_by_repo.items():
            logger.info(f"Processing repository: {repo_name} with {len(repo_deployments)} deployments")

            repo_info = next((r for r in repositories if r.get("name") == repo_name), None)
            if not repo_info:
                raise Exception(f"Repository configuration not found: {repo_name}")

            await self._process_repository_manifests(repo_info, repo_deployments, project_data)

        logger.info(f"Successfully processed all application manifests for {project_name}")

    async def _process_repository_manifests(
        self,
        repo_config: dict[str, Any],
        deployments: list[dict[str, Any]],
        project_data: dict[str, Any],
    ) -> None:
        """
        Process manifests for a specific repository.

        Args:
            repo_config: Repository configuration
            deployments: List of deployments for this repository
            project_data: The parsed project data

        Returns:
            True if manifests were processed successfully, False otherwise
        """
        project_repo_connector = await self.get_git_connector_for_deployment(repo_config["name"], repo_config)
        # TODO: rethink if all deployments should be handled or only the current cluster
        for deployment in (d for d in deployments if d.get("cluster") == settings.CLUSTER_MANAGER):
            await self._process_deployment_manifests(deployment, project_data, project_repo_connector)
            await project_repo_connector.commit_changes(
                f"Add kubernetes manifests for project {project_data['name']} for {deployment['name']}"
            )

        await project_repo_connector.push_changes()

        logger.info(f"Successfully processed repository: {repo_config['name']}")

    # TODO: maybe this should be moved to project_file manager
    async def get_repository_path(self, repository_name: str) -> str | None:
        project_data = await self.get_contents()
        repositories = project_data.get("repositories") or []
        repositories: list[dict[str, str]] = repositories
        for repo in repositories:
            if repo.get("name") == repository_name:
                return repo.get("path", "")
        return ""

    async def _process_deployment_manifests(
        self,
        deployment: dict[str, Any],
        project_data: dict[str, Any],
        git_connector: GitConnector,
    ) -> None:
        """
        Process manifests for a specific deployment.

        Args:
            deployment: Deployment configuration
            project_data: The parsed project data
            git_connector: GitConnector for the repository

        Returns:
            True if deployment manifests were processed successfully, False otherwise
        """
        project_name = project_data.get("name")
        deployment_name = deployment.get("name")
        cluster_name = deployment["cluster"]

        repo_path = await self.get_repository_path(deployment["repository"])
        if repo_path:
            deployment_path = f"{repo_path}/{cluster_name}/{project_name}/{deployment_name}"
        else:
            deployment_path = f"{cluster_name}/{project_name}/{deployment_name}"

        prefixed_namespace = get_prefixed_namespace(cluster_name, deployment["namespace"])

        logger.info(f"Processing deployment: {deployment_name} at path: {deployment_path}")

        await self.create_application_manifests(deployment, project_data, git_connector, deployment_path)

        # Note: SSO and user secrets are already created in create_application_manifests above

        # Create a kustomization file BEFORE encrypting .to-sops.yaml files
        # This ensures kustomization and decrypt-sops.yaml can see all .to-sops.yaml files
        target_path = os.path.join(await git_connector.get_working_dir(), deployment_path)
        sops_files, regular_files = self._manifest_generator.collect_manifest_files(
            target_path, include_subfolders=False
        )
        logger.info(f"Found {len(sops_files)} SOPS files and {len(regular_files)} regular files for kustomization")
        await self.create_kustomization_file(
            git_connector, prefixed_namespace, sops_files, regular_files, deployment_path, deployment
        )

        # FINAL STEP: Convert .to-sops.yaml files to .sops.yaml files
        # This must be done AFTER kustomization creation so that decrypt-sops.yaml
        # can reference the original .to-sops.yaml filenames
        public_key = get_project_public_key(project_data)
        logger.info(f"Encrypting .to-sops.yaml files for deployment: {deployment_name}")
        logger.info(f"Using SOPS public key for namespace: {prefixed_namespace}")
        logger.info(f"SOPS encryption target path: {target_path}")

        # List .to-sops.yaml files before encryption for debugging
        import glob

        to_sops_pattern = os.path.join(target_path, "*.to-sops.yaml")
        to_sops_files = glob.glob(to_sops_pattern)
        logger.info(f"Found {len(to_sops_files)} .to-sops.yaml files for final encryption:")
        for file_path in to_sops_files:
            logger.info(f"  - {os.path.basename(file_path)}")

        encrypt_to_sops_files(target_path, public_key)

        # Verify all files were encrypted
        remaining_to_sops_files = glob.glob(to_sops_pattern)
        if remaining_to_sops_files:
            logger.warning(f"Found {len(remaining_to_sops_files)} .to-sops.yaml files that were NOT encrypted:")
            for file_path in remaining_to_sops_files:
                logger.warning(f"  - UNENCRYPTED: {os.path.basename(file_path)}")
        else:
            logger.info("All .to-sops.yaml files successfully encrypted")

    async def close(self) -> None:
        await self.close_git_connector_for_project_files()
        await self.close_git_connector_for_argocd()
        await self.close_git_connectors_for_deployments()
        if self._database_manager:
            await self._database_manager.close()

    async def process_project(self, deployment_name: str | None = None) -> None:
        """
        Process the project file and create all required resources.

        Args:
            deployment_name: Optional deployment name to process only specific deployment
        """
        logger.info(f"Processing project file: {self._project_file_relative_path}")

        try:
            project_data = await self.get_contents()
            project_name = project_data.get("name")
            logger.info(
                f"Processing project: {project_name} and deployment {deployment_name if deployment_name else 'all'}"
            )

            if not await self.has_deployments_for_current_cluster():
                logger.info(f"Project '{project_name}' cluster validation failed - skipping processing")
                return

            # # 1.5. Create configuration handler to collect deployment info
            # config_handler = create_configuration_handler(project_name, self.project_data)

            # 2. Extract changes (dummy for now - all marked as "added")
            self._extract_added_changes(project_data)

            # TODO: most likely remove creating repositories
            # 3. Process repositories
            # repositories = self._get_project_repositories(added_changes)
            # missing_repos = await self._get_missing_repositories(repositories)
            # if missing_repos and not await self._create_repositories(missing_repos, project_data):
            #     logger.error("Failed to create repositories, aborting")
            #     return False

            progress_manager = self.get_progress_manager()
            creation_task = None

            if progress_manager:
                creation_task = progress_manager.add_task("Project creation")

            # Create namespaces first (always first task)
            await self.check_and_create_namespaces(deployment_name)

            await self.check_and_create_sops_secrets_in_namespaces(deployment_name)

            # Create service resources using service managers
            deployments = project_data.get("deployments", [])

            # Filter deployments if specific deployment_name is provided
            if deployment_name:
                deployments = [d for d in deployments if d.get("name") == deployment_name]
                logger.info(f"Processing only deployment: {deployment_name}")

            for deployment in deployments:
                if deployment.get("cluster") == settings.CLUSTER_MANAGER:
                    await self._database_manager.create_resources_for_deployment(project_data, deployment)
                    await self._minio_manager.create_resources_for_deployment(project_data, deployment)
                    await self._keycloak_manager.create_resources_for_deployment(project_data, deployment)

            await self._process_application_manifests(deployment_name)

            # Save encrypted deployment configurations to project file
            try:
                await self._save_encrypted_configs_to_project_file()
            except Exception as e:
                logger.warning(f"Failed to save encrypted configs, continuing: {e}")

            # TODO: this may need to be done earlier.. or at another place
            await (await self.get_git_connector_for_project_files()).commit_and_push(f"Adding project {project_name}")

            await self.create_argocd_resources(deployment_name)

            # Register the project with decrypted configuration data
            api_key = await self.get_api_key()
            project_name = await self.get_name()
            project_service = get_project_service()
            filename = (
                os.path.basename(self._project_file_relative_path)
                if self._project_file_relative_path
                else f"{project_name}.yaml"
            )

            # Get project data with decrypted configurations for display
            project_data_with_configs = await self._get_project_data_with_decrypted_configs()

            # Extract users from project data
            users_data = project_data_with_configs.get("users", [])
            users = []
            if users_data and isinstance(users_data, list):
                from opi.services.project_service import ProjectUser

                for user_data in users_data:
                    if isinstance(user_data, dict) and "email" in user_data and "role" in user_data:
                        users.append(ProjectUser(email=user_data["email"], role=user_data["role"]))

            project_service.register(
                project_name,
                api_key,
                filename,
                users=users if users else None,
                data=project_data_with_configs,
            )

            if progress_manager and creation_task:
                self.get_progress_manager().complete_task(creation_task)
        except Exception as e:
            logger.exception(f"Error: {e}")
        finally:
            pass
            # TODO: we may need to close it here, but the project manager is still used in a flow which should change
            # await self.close()

    async def create_application_manifests(
        self,
        deployment: dict[str, Any],
        project_data: dict[str, Any],
        git_connector: GitConnector,
        target_dir: str | None = None,
    ) -> list[str]:
        """
        Create application manifests (deployment, service, ingress) in the git repository.
        This creates the application manifest files for each component in each deployment.

        Args:
            deployment: current deployment
            project_data: Dictionary containing project configuration
            git_connector: The git connector with an already cloned repository
            target_dir: Optional subdirectory within the git repository

        Returns:
            List of created manifest filenames, empty list if failed
        """

        working_dir = await git_connector.get_working_dir()

        project_name = project_data.get("name")
        logger.info(f"Creating application manifests for project: {project_name}")

        created_files = []

        deployment_name = deployment["name"]
        cluster = deployment["cluster"]
        namespace = get_prefixed_namespace(cluster, deployment["namespace"])

        logger.info(f"Processing deployment: {deployment_name} in prefixed namespace: {namespace}")

        # Check if deployment has components
        components = deployment.get("components", [])
        if not components:
            logger.warning(f"No components found in deployment {deployment_name}, skipping")
            return []

        # Process each component within the deployment
        for component in components:
            # Get component reference and image from deployment
            component_reference = component.get("reference")
            image_url = component.get("image", "nginxdemos/hello")

            if not component_reference:
                logger.warning(f"Component missing reference in deployment {deployment_name}, skipping")
                continue

            component_name = component_reference

            # Update component deployment name if progress manager is available
            progress_manager = self.get_progress_manager()
            if progress_manager:
                deployment_resource_name = f"{project_name}-{component_name}"
                progress_manager.update_component_deployment(component_name, deployment_resource_name)
                logger.debug(f"Updated component {component_name} deployment name to {deployment_resource_name}")

            # Extract the application port from the component definition using the file handler
            application_port = self._project_file_handler.extract_component_port(
                project_data, component_reference, default_port=80
            )

            # Extract storage configuration from component
            storage_configs = self._project_file_handler.extract_component_storage(project_data, component_reference)

            # Extract publish-on-web flag from component
            publish_on_web = self._project_file_handler.extract_component_publish_on_web(
                project_data, component_reference
            )

            # Extract user environment variables from component
            user_env_vars = await self._project_file_handler.extract_component_user_env_vars(
                project_data, component_reference
            )

            # Extract deployment-level env-vars and merge with component-level user-env-vars
            deployment_env_vars = component.get("env-vars", {})
            if deployment_env_vars:
                logger.info(
                    f"Found {len(deployment_env_vars)} deployment-level env-vars for component: {component_name}"
                )
                # Deployment-level env-vars override component-level user-env-vars
                user_env_vars.update(deployment_env_vars)

            # Add unique names to storage configs for templating
            processed_storage_configs = []
            for i, storage in enumerate(storage_configs):
                storage_copy = storage.copy()
                # Generate unique storage name based on mount path or index using centralized utility
                mount_path = storage.get("mount-path", f"/storage-{i}")
                storage_name = generate_storage_name(mount_path, i)
                storage_copy["name"] = storage_name
                processed_storage_configs.append(storage_copy)

            # Create unique name combining deployment name and component name using centralized utility
            # Project name is not included since resources are deployed within project-specific namespaces
            unique_name = generate_unique_name(deployment_name, component_name)

            # Generate ingress map based on cluster configuration and optional subdomain using centralized utility
            ingress_postfix = get_ingress_postfix(cluster)
            subdomain = deployment.get("subdomain")
            logger.info(f"Extracted subdomain for {component_name}: {subdomain}")
            ingress_map = generate_ingress_map(
                component_name, deployment_name, project_name, ingress_postfix, subdomain
            )
            logger.info(f"Generated ingress_map for {component_name}: {ingress_map}")
            # Use default hostname for backward compatibility
            hostname = next(iter(ingress_map.values()))
            logger.info(f"Primary hostname for {component_name}: {hostname}")

            # Update component web address if progress manager is available and hostname exists
            if progress_manager and hostname:
                # Construct full URL using proper naming function
                web_address = generate_public_url(hostname)
                progress_manager.update_component_web_address(component_name, web_address)
                logger.debug(f"Updated component {component_name} web address to {web_address}")

            # Generate environment variables using service-based registration pattern
            env_vars = {}

            # Register storage environment variables using service definitions
            if storage_configs:
                storage_env_vars = self._generate_storage_env_vars_from_services(processed_storage_configs)
                if storage_env_vars:
                    env_vars.update(storage_env_vars)
                    self._register_env_var(deployment_name, component_name, "storage", storage_env_vars)

            # Register publish-on-web environment variables using service definitions
            if publish_on_web and hostname:
                web_env_vars = self._generate_web_env_vars_from_services(hostname)
                if web_env_vars:
                    env_vars.update(web_env_vars)
                    self._register_env_var(deployment_name, component_name, "web", web_env_vars)

            # Register user environment variables
            # NOTE: User env vars go into a secret and are referenced via envFrom, not as direct env vars
            if user_env_vars:
                self._register_env_var(deployment_name, component_name, "user", user_env_vars)

            # # IMPORTANT: Add component FIRST to prevent fallback creation with namespace=None
            # if config_handler:
            #     logger.debug(f"Config DEBUG: Adding component {component_name} with namespace: {namespace}")
            #     config_handler.add_component(component_name, "component", namespace)

            # Process SSO-Rijk option if present
            env_from_secrets = []
            sso_config = None
            if await self._should_process_sso_rijk(project_data, component_reference):
                logger.info(f"Processing SSO-Rijk for component: {component_name}")
                ingress_hosts_for_sso = list(ingress_map.values())
                logger.info(f"Sending ingress_hosts to SSO setup: {ingress_hosts_for_sso}")
                # Using secrets map to store and link secret information
                sso_config = await self._setup_sso_rijk_integration(
                    project_name, component_name, deployment_name, namespace, hostname, ingress_hosts_for_sso
                )

                # Add Keycloak secret to envFrom list when SSO is enabled
                # NOTE: Keycloak secret is per-deployment, not per-component
                if sso_config:
                    keycloak_secret_name = KeycloakSecret.get_secret_name(deployment_name)
                    env_from_secrets.append(keycloak_secret_name)
                    logger.debug(f"Keycloak secret added to envFrom: {keycloak_secret_name}")

            # Process user environment variables if present
            if user_env_vars:
                logger.info(
                    f"Processing {len(user_env_vars)} user environment variables for component: {component_name}"
                )

                user_secret_name = UserSecret.get_secret_name(unique_name)
                env_from_secrets.append(user_secret_name)
                logger.debug(f"User secret added to envFrom: {user_secret_name}")

            # Check if this component uses PostgreSQL service and add database secret
            component_uses_postgresql = False
            if component_reference:
                component_query = jsonpath_parse(f"$.components[?@.name=='{component_reference}']['uses-services']")
                component_services = [match.value for match in component_query.find(project_data)]
                # Flatten the services list (in case it's nested)
                all_services = []
                for services in component_services:
                    if isinstance(services, list):
                        all_services.extend(services)
                    else:
                        all_services.append(services)
                component_uses_postgresql = ServiceType.POSTGRESQL_DATABASE.value in all_services

            # Check if this component uses MinIO service and add object storage secret
            component_uses_minio = False
            if component_reference:
                component_query = jsonpath_parse(f"$.components[?@.name=='{component_reference}']['uses-services']")
                component_services = [match.value for match in component_query.find(project_data)]
                # Flatten the services list (in case it's nested)
                all_services = []
                for services in component_services:
                    if isinstance(services, list):
                        all_services.extend(services)
                    else:
                        all_services.append(services)
                component_uses_minio = ServiceType.MINIO_STORAGE.value in all_services

            # Add database secret to envFrom list when PostgreSQL is used
            # Use deployment-level naming for database secrets (shared between components)
            if component_uses_postgresql:
                database_secret_name = DatabaseSecret.get_secret_name(deployment_name)
                env_from_secrets.append(database_secret_name)
                logger.debug(f"Database secret added to envFrom: {database_secret_name}")

            # Add MinIO secret to envFrom list when object storage is used
            # Use deployment-level naming for MinIO secrets (shared between components)
            if component_uses_minio:
                minio_secret_name = MinIOSecret.get_secret_name(deployment_name)
                env_from_secrets.append(minio_secret_name)
                logger.debug(f"MinIO secret added to envFrom: {minio_secret_name}")

            pod_replacement_mode = (
                "Recreate" if any(item.get("type") == "persistent" for item in storage_configs) else "RollingUpdate"
            )

            # Prepare secret_pairs for config hash from all secrets in secrets map
            secret_pairs = {}
            deployment_secrets = self._secrets_to_create.get(deployment_name, {})
            for secret_type, secret_data in deployment_secrets.items():
                if isinstance(secret_data, dict):
                    for key, value in secret_data.items():
                        secret_pairs[f"{secret_type}_{key}"] = value

            # Generate configuration hash for deployment reload trigger (includes all secret data)
            config_hash = self._generate_config_hash(env_vars, env_from_secrets, user_env_vars, secret_pairs)

            # Prepare variables for templating
            variables = {
                "name": unique_name,
                "namespace": namespace,
                "hostname": hostname,
                "project": {"name": project_name},
                "cluster": cluster,  # Add cluster information for template conditionals
                "pod_replacement_mode": pod_replacement_mode,
                "imageURL": image_url,
                "application_port": application_port,
                "service_port": application_port,  # Use same port for service by default
                "storage_configs": processed_storage_configs,
                "env_vars": env_vars,
                "env_from_secrets": env_from_secrets,  # List of secrets for envFrom
                "secret_pairs": secret_pairs,  # Pass OIDC values through secret_pairs
                "config_hash": config_hash,  # Hash for triggering deployment reload on config changes
                # Cluster-specific ingress configuration
                "enable_tls": get_ingress_tls_enabled(cluster),
                "ip_whitelist": get_ingress_ip_whitelist(cluster),
            }

            logger.info(f"Creating manifests for component: {component_name} with image: {image_url}")

            # Collect additional configuration information if handler is provided
            # if config_handler:
            # Component was already added above before SSO processing

            # Add environment variables
            # for env_key, env_value in env_vars.items():
            #     config_handler.add_env_var(component_name, env_key, str(env_value))
            #
            # # Add web address (hostname) only if publish-on-web is enabled
            # if publish_on_web:
            #     web_address = f"https://{hostname}"
            #     logger.info(f"Adding web address to config: {component_name} -> {web_address}")
            #     config_handler.add_web_address(component_name, web_address)
            # else:
            #     logger.debug(f"Skipping web address for {deployment_name}-{component_name} (publish-on-web: false)")

            # Add storage configuration
            # for storage_config in processed_storage_configs:
            #     config_handler.add_storage_config(
            #         component_name,
            #         "pvc",
            #         {
            #             "name": storage_config.get("name"),
            #             "mount_path": storage_config.get("mount-path"),
            #             "size": storage_config.get("size"),
            #             "access_mode": storage_config.get("access-mode"),
            #         },
            #     )

            # Add custom configuration
            # config_handler.add_custom_config(component_name, "image", image_url)
            # config_handler.add_custom_config(component_name, "port", application_port)
            # config_handler.add_custom_config(component_name, "unique_name", unique_name)

            # Create each manifest type in the git repository
            manifests = ["deployment.yaml.jinja", "service.yaml.jinja", "allow-all-network-policy.yaml.jinja"]

            # Add ingress manifest only if publish-on-web is enabled for this component
            if publish_on_web:
                manifests.append("ingress.yaml.jinja")
                logger.info(f"Including ingress manifest for component '{component_name}' (publish-on-web: true)")
            else:
                logger.debug(f"Skipping ingress manifest for component '{component_name}' (publish-on-web: false)")

            # Construct the full output directory path once for reuse
            if target_dir:
                # target_dir already contains the complete path structure (cluster/project/deployment)
                full_output_dir = os.path.join(working_dir, target_dir)
            else:
                # Only add project_name/deployment_name when no target_dir is provided
                full_output_dir = os.path.join(working_dir, project_name, deployment_name)

            for manifest_file in manifests:
                manifest_path = os.path.join(os.path.dirname(__file__), "..", "..", "manifests", manifest_file)

                if not os.path.exists(manifest_path):
                    logger.warning(f"Manifest file not found: {manifest_path}")
                    continue

                # Use enhanced manifest generator for proper directory structure
                # Extract just the manifest name (without .yaml.jinja extension)
                manifest_name = manifest_file.replace(".yaml.jinja", "")

                # Handle ingress manifests - iterate through ingress_map for both single and multiple ingresses
                if manifest_name == "ingress":
                    for ingress_name, ingress_hostname in ingress_map.items():
                        # Create unique manifest name
                        unique_manifest_name = generate_manifest_name(component_name, manifest_name)

                        # Create ingress-specific variables
                        ingress_variables = variables.copy()
                        ingress_variables.update(
                            {
                                "name": ingress_name,  # Unique ingress resource name
                                "service_name": unique_name,  # Service name stays the same
                                "hostname": ingress_hostname,
                            }
                        )

                        # Create the ingress manifest file
                        manifest_file_path = self._manifest_generator.create_manifest_file(
                            template_path=manifest_path,
                            values=ingress_variables,
                            output_dir=full_output_dir,
                            output_filename=unique_manifest_name,
                            use_sops=False,
                        )
                        created_files.append(f"{unique_manifest_name}.yaml")
                        logger.info(
                            f"Successfully created {manifest_file} manifest for {ingress_hostname}: {manifest_file_path}"
                        )
                else:
                    # Standard single manifest creation
                    unique_manifest_name = generate_manifest_name(component_name, manifest_name)

                    # Use SOPS encryption for generic secrets (SSO/OIDC), regular processing for others
                    use_sops_for_manifest = manifest_name == "generic-secret"

                    # Create manifest file in the specific directory structure
                    manifest_file_path = self._manifest_generator.create_manifest_file(
                        template_path=manifest_path,
                        values=variables,
                        output_dir=full_output_dir,
                        output_filename=unique_manifest_name,
                        use_sops=use_sops_for_manifest,
                    )

                    # Add to the list of created files with component name for uniqueness
                    created_files.append(f"{unique_manifest_name}.yaml")
                    logger.info(f"Successfully created {manifest_file} manifest: {manifest_file_path}")

            # Create PVC manifests for persistent storage
            persistent_storage = self._project_file_handler.get_persistent_storage(processed_storage_configs)

            if persistent_storage:
                logger.info(f"Creating {len(persistent_storage)} PVC manifests for component: {component_name}")

                # Get cluster storage configuration
                storage_class_name = get_storage_class_name(cluster)
                access_modes = get_storage_access_modes(cluster)

                pvc_template_path = os.path.join(os.path.dirname(__file__), "..", "..", "manifests", "pvc.yaml.jinja")

                for storage in persistent_storage:
                    # Prepare PVC variables using centralized naming utility
                    pvc_variables = {
                        "name": generate_pvc_name(unique_name, storage["name"]),
                        "namespace": namespace,
                        "size": storage.get("size", "10Gi"),
                        "storage_class_name": storage_class_name,
                        "access_modes": access_modes,
                    }

                    # Handle clone-from logic for PVC
                    clone_from = deployment.get("clone-from")
                    if clone_from:
                        # Generate source PVC name using the same naming convention
                        source_unique_name = generate_unique_name(clone_from, component_name)
                        source_pvc_name = generate_pvc_name(source_unique_name, storage["name"])
                        pvc_variables["source_pvc_name"] = source_pvc_name
                        logger.info(f"PVC {pvc_variables['name']} will be cloned from {source_pvc_name}")

                    # Create PVC manifest using centralized naming utility
                    pvc_manifest_name = generate_manifest_name(component_name, f"{storage['name']}-pvc")

                    pvc_manifest_path = self._manifest_generator.create_manifest_file(
                        template_path=pvc_template_path,
                        values=pvc_variables,
                        output_dir=full_output_dir,
                        output_filename=pvc_manifest_name,
                        use_sops=False,
                    )

                    created_files.append(f"{pvc_manifest_name}.yaml")
                    logger.info(f"Successfully created PVC manifest: {pvc_manifest_path}")

            # Create separate secret manifests for SSO and user secrets
            secret_template_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "manifests", "generic-secret.yaml.to-sops.jinja"
            )

            # Create SSO secret if Keycloak credentials are available in secrets map
            keycloak_credentials = self._get_secret_from_map(deployment_name, "keycloak", KeycloakSecret)
            if keycloak_credentials:
                # Always use SOPS encryption for secrets (security requirement)
                use_sops_for_sso = True

                # Use the existing Keycloak secret instance directly (no need to recreate)
                keycloak_secret = keycloak_credentials

                # Only include fields needed for the generic-secret template
                # NOTE: Keycloak secret is per-deployment, not per-component
                sso_secret_vars = {
                    "name": KeycloakSecret.get_secret_name(deployment_name),
                    "namespace": namespace,
                    "secret_type": "keycloak",  # For proper labeling
                    "secret_pairs": keycloak_secret.to_k8s_secret_data(),
                }

                # Create SSO secret with keycloak naming convention (deployment-level)
                sso_manifest_name = generate_manifest_name(deployment_name, "keycloak-secret")

                sso_secret_path = self._manifest_generator.create_manifest_file(
                    template_path=secret_template_path,
                    values=sso_secret_vars,
                    output_dir=full_output_dir,
                    output_filename=sso_manifest_name,
                    use_sops=use_sops_for_sso,
                )

                # All secrets are SOPS encrypted for security
                sops_filename = f"{sso_manifest_name}.to-sops.yaml"
                created_files.append(sops_filename)
                logger.info(f"SSO secret will be SOPS encrypted: {sops_filename}")
                logger.info(f"Successfully created SSO secret manifest: {sso_secret_path}")

            # Create user secret if configured
            if user_env_vars:
                logger.debug(f"Processing {len(user_env_vars)} user environment variables for {component_name}")

                # Create typed User secret
                user_secret = UserSecret(env_vars=user_env_vars)

                # Only include fields needed for the generic-secret template
                user_secret_vars = {
                    "name": UserSecret.get_secret_name(unique_name),
                    "namespace": namespace,
                    "secret_type": "user",  # For proper labeling
                    "secret_pairs": user_secret.to_k8s_secret_data(),
                }

                # Create user secret with user naming convention
                user_manifest_name = generate_manifest_name(component_name, "user-secret")
                # Always use SOPS encryption for secrets (security requirement)
                use_sops_for_user = True

                user_secret_path = self._manifest_generator.create_manifest_file(
                    template_path=secret_template_path,
                    values=user_secret_vars,
                    output_dir=full_output_dir,
                    output_filename=user_manifest_name,
                    use_sops=use_sops_for_user,
                )

                # All secrets are SOPS encrypted for security
                sops_filename = f"{user_manifest_name}.to-sops.yaml"
                created_files.append(sops_filename)
                logger.info(f"User secret will be SOPS encrypted: {sops_filename}")
                logger.info(f"Successfully created user secret manifest: {user_secret_path}")

            # Create database secret if component uses PostgreSQL service
            if component_uses_postgresql:
                db_credentials = self._get_secret_from_map(deployment_name, "database", DatabaseSecret)

                if db_credentials:
                    logger.debug(f"Creating database secret for {component_name} with PostgreSQL credentials")

                    # Get cluster-specific database server hostname
                    database_server_host = get_database_server(cluster)

                    # Create typed Database secret with cluster-specific host
                    database_secret = DatabaseSecret(
                        host=database_server_host,  # Use cluster-specific host
                        port=db_credentials.port,
                        username=db_credentials.username,
                        password=db_credentials.password,
                        database=db_credentials.database,
                        schema=db_credentials.schema,
                    )

                    # Store the updated secret instance for configuration tracking
                    self._add_secret_to_create(deployment_name, "database", database_secret)

                    # Create database secret vars with all required environment variables
                    # Use deployment-level naming for the secret name
                    database_secret_vars = {
                        "name": DatabaseSecret.get_secret_name(deployment_name),
                        "namespace": namespace,
                        "secret_pairs": database_secret.to_k8s_secret_data(),
                    }

                    # Create database secret with deployment-level naming (not component-level)
                    # This matches what we look for in _get_existing_database_credentials_from_k8s
                    database_manifest_name = f"{DatabaseSecret.get_secret_name(deployment_name)}-secret"

                    database_secret_path = self._manifest_generator.create_manifest_file(
                        template_path=secret_template_path,
                        values=database_secret_vars,
                        output_dir=full_output_dir,
                        output_filename=database_manifest_name,
                        use_sops=True,
                    )

                    # All secrets are SOPS encrypted for security
                    sops_filename = f"{database_manifest_name}.to-sops.yaml"
                    created_files.append(sops_filename)
                    logger.info(f"Database secret will be SOPS encrypted: {sops_filename}")
                    logger.info(f"Successfully created database secret manifest: {database_secret_path}")
                else:
                    logger.warning(
                        f"Component {component_name} uses PostgreSQL but no database credentials found in deployment {deployment_name}"
                    )

            # Create MinIO secret if component uses object storage service
            if component_uses_minio:
                # Get MinIO credentials from the private secrets map (not from deployment data)
                minio_credentials = self._get_secret_from_map(deployment_name, "minio", MinIOSecret)

                if minio_credentials:
                    logger.debug(f"Creating MinIO secret for {component_name} with object storage credentials")

                    minio_server_host = get_minio_server(cluster)

                    # Create typed MinIO secret with cluster-specific host
                    minio_secret = MinIOSecret(
                        host=minio_server_host,  # Use cluster-specific host
                        access_key=minio_credentials.access_key,
                        secret_key=minio_credentials.secret_key,
                        bucket_name=minio_credentials.bucket_name,
                        region=minio_credentials.region,
                    )

                    # Create MinIO secret vars with all required environment variables
                    # Use deployment-level naming for the secret name
                    minio_secret_vars = {
                        "name": MinIOSecret.get_secret_name(deployment_name),
                        "namespace": namespace,
                        "secret_pairs": minio_secret.to_k8s_secret_data(),
                    }

                    # Create MinIO secret with deployment-level naming (not component-level)
                    # This matches what we look for in _get_existing_minio_credentials_from_k8s
                    minio_manifest_name = f"{MinIOSecret.get_secret_name(deployment_name)}-secret"

                    minio_secret_path = self._manifest_generator.create_manifest_file(
                        template_path=secret_template_path,
                        values=minio_secret_vars,
                        output_dir=full_output_dir,
                        output_filename=minio_manifest_name,
                        use_sops=True,
                    )

                    # All secrets are SOPS encrypted for security
                    sops_filename = f"{minio_manifest_name}.to-sops.yaml"
                    created_files.append(sops_filename)
                    logger.info(f"MinIO secret will be SOPS encrypted: {sops_filename}")
                    logger.info(f"Successfully created MinIO secret manifest: {minio_secret_path}")
                else:
                    logger.warning(
                        f"Component {component_name} uses MinIO but no object storage credentials found in deployment {deployment_name}"
                    )

        return created_files

    # TODO: this should be moved to manifests.py
    async def create_kustomization_file(
        self,
        git_connector: GitConnector,
        namespace: str,
        sops_files: list[str],
        regular_files: list[str],
        target_dir: str | None = None,
        deployment: dict[str, Any] | None = None,
    ) -> bool:
        """
        Create a kustomization.yaml file that includes both SOPS encrypted files and regular files.
        Uses the new manifest generator with YAML templates.

        Args:
            git_connector: The git connector with an already cloned repository
            namespace: Target namespace for the kustomization
            sops_files: List of SOPS encrypted file names
            regular_files: List of regular manifest file names
            target_dir: Optional target directory within the git repository
            deployment: Optional deployment data containing cluster information for namespace prefixing

        Returns:
            True if kustomization file was created successfully, False otherwise
        """
        working_dir = await git_connector.get_working_dir()
        if target_dir:
            target_path = os.path.join(working_dir, target_dir)
            os.makedirs(target_path, exist_ok=True)
        else:
            target_path = working_dir

        # Use the manifest generator to create kustomization files
        result = self._manifest_generator.create_kustomization_files(
            output_dir=target_path,
            namespace=namespace,
            sops_files=sops_files,
            regular_files=regular_files,
            deployment=deployment,
        )

        if result:
            logger.info(
                f"Successfully created kustomization.yaml with {len(sops_files)} SOPS files and {len(regular_files)} regular files"
            )

        return result

    async def _create_argocd_kustomization_file(self) -> None:
        """
        Create a kustomization.yaml file for ArgoCD project folders.
        This method lists all YAML files in the project folder and creates a kustomization.yaml
        that includes all ArgoCD manifests (applications, repositories, appprojects).
        Uses the new manifest generator with YAML templates.
        """

        git_connector_for_argocd = await self.get_git_connector_for_argocd()
        working_dir = await git_connector_for_argocd.get_working_dir()

        project_data = await self.get_contents()
        project_name = project_data.get("name")

        deployments = project_data.get("deployments", [])
        clusters_used = set()
        for deployment in deployments:
            cluster_name = deployment.get("cluster", "local")
            clusters_used.add(cluster_name)

        for cluster_name in clusters_used:
            project_dir = os.path.join(str(working_dir), str(cluster_name), str(project_name))

            self._manifest_generator.create_kustomization_files(
                output_dir=project_dir,
                namespace=get_argo_namespace(cluster_name),  # Use ArgoCD namespace for the cluster
            )

            logger.info(
                f"Successfully created ArgoCD kustomization.yaml for project {project_name} for cluster {cluster_name}"
            )

    async def get_contents(self) -> dict[str, Any]:
        """
        Convenience method to get the contents of the project file.
        :return: Contents of the project file
        """
        full_path = await self.get_project_full_file_path()
        self.__has_contents = True
        return await self._project_file_handler.read_project_file(full_path)

    async def _get_by_json_path(self, json_path: str) -> Any:
        """
        Get a value from the project file using JSONPath.

        Args:
            json_path: JSONPath expression to query the project data

        Returns:
            The value found at the JSONPath, or None if not found
        """
        project_data = await self.get_contents()

        try:
            jsonpath_expr = jsonpath_parse(json_path)
            matches = jsonpath_expr.find(project_data)
            return matches[0].value if matches else None
        except Exception as e:
            raise Exception(f"Error querying JSONPath '{json_path}") from e

    async def get_api_key(self) -> str:
        """
        Get and decrypt the project's API key.

        Returns:
            Decrypted API key
        """
        project_name = await self.get_name()
        encrypted_api_key = await self._get_by_json_path("$.config.api-key")
        if not encrypted_api_key:
            raise ValueError(f"No api key found in project config for {project_name}")
        private_key = await get_decoded_project_private_key(await self.get_contents())
        decrypted_api_key = await decrypt_age_content(str(encrypted_api_key), private_key)
        logger.debug(f"Successfully decrypted API key for project: {project_name}")
        return decrypted_api_key

    async def _should_process_sso_rijk(self, project_data: dict[str, Any], component_reference: str) -> bool:
        """
        Check if a component has the sso-rijk option enabled.

        Args:
            project_data: The project configuration data
            component_reference: The component reference name

        Returns:
            True if sso-rijk should be processed, False otherwise
        """
        try:
            components = project_data.get("components", [])
            for component in components:
                if component.get("name") == component_reference:
                    # Check uses-services array for SSO
                    uses_services = component.get("uses-services", [])
                    component_services = ServiceAdapter.parse_services_from_strings(uses_services)
                    has_sso_service = ServiceType.SSO_RIJK in component_services

                    if has_sso_service:
                        logger.debug(f"Component {component_reference} has sso-rijk enabled")
                        return True
                    break

            logger.debug(f"Component {component_reference} does not have sso-rijk enabled")
            return False

        except Exception as e:
            logger.exception(f"Error checking sso-rijk option for component {component_reference}: {e}")
            return False

    async def _get_keycloak_credentials_from_config(
        self, project_data: dict[str, Any], deployment_name: str, project_name: str
    ) -> dict[str, Any] | None:
        """
        Retrieve existing Keycloak credentials from project config.

        NOTE: This is for backwards compatibility only. In the new architecture,
        deployment client credentials are stored in K8s secrets, NOT in project config.
        The config.keycloak field is now a list containing project admin credentials only.

        Args:
            project_data: The project configuration data
            deployment_name: Name of the deployment
            project_name: Name of the project (used for logging/validation)

        Returns:
            None (deployment credentials are no longer stored in config)
        """
        # Deployment credentials are stored in K8s secrets, not in project config
        # This method is kept for backwards compatibility but always returns None
        logger.debug(f"Deployment credentials for '{deployment_name}' are stored in K8s secrets, not in project config")
        return None

    async def _store_keycloak_credentials_in_config(
        self, client_info: dict[str, Any], encrypted_client_secret: str
    ) -> None:
        """
        Store Keycloak credentials in the project config.

        NOTE: This method is deprecated and no longer used. In the new architecture,
        deployment client credentials are stored in K8s secrets only.
        The config.keycloak field is now a list containing project admin credentials only.

        Args:
            client_info: Client information from Keycloak
            encrypted_client_secret: AGE-encrypted client secret
        """
        # Deployment credentials are stored in K8s secrets, not in project config
        # This method is kept for backwards compatibility but does nothing
        logger.debug("Deployment credentials are stored in K8s secrets, not storing in project config")

    async def _setup_project_keycloak_realm(self, project_name: str, cluster: str, keycloak_url: str) -> dict[str, Any]:
        """
        Set up project-level Keycloak infrastructure for a cluster.

        This creates the project realm, admin user, and federation with RIG Platform.

        Steps:
        1. Generate admin username/password
        2. Encrypt password with project's AGE public key
        3. Create project realm in master Keycloak
        4. Create project admin user in master realm
        5. Assign realm-admin role to admin for project realm
        6. Create project client in RIG Platform realm
        7. Add RIG Platform as IDP in project realm
        8. Configure SSO-only authentication
        9. Store config in project.yaml
        10. Save project file

        Args:
            project_name: Name of the project
            cluster: Name of the cluster
            keycloak_url: Base URL of the Keycloak server

        Returns:
            Dictionary with host, realm, username, password (encrypted)
        """
        from ruamel.yaml.scalarstring import LiteralScalarString

        from opi.connectors.keycloak import create_keycloak_connector
        from opi.utils.age import encrypt_age_content, get_project_public_key
        from opi.utils.naming import (
            generate_project_admin_username,
            generate_project_platform_client_id,
            generate_project_realm_name,
        )
        from opi.utils.passwords import generate_secure_password

        logger.info(f"Setting up project Keycloak realm for {project_name} in cluster {cluster}")

        # Generate names
        admin_username = generate_project_admin_username(project_name, cluster)
        realm_name = generate_project_realm_name(project_name, cluster)
        platform_client_id = generate_project_platform_client_id(project_name, cluster)

        # Generate and encrypt password
        admin_password = generate_secure_password()
        project_data = await self.get_contents()
        project_public_key = get_project_public_key(project_data)

        if not project_public_key:
            raise Exception(f"Project public key not found for {project_name}")

        encrypted_password = await encrypt_age_content(admin_password, project_public_key)
        encrypted_password_str = LiteralScalarString(encrypted_password)

        # Create Keycloak connector
        keycloak = await create_keycloak_connector(
            keycloak_url=keycloak_url,
            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
        )

        # IDEMPOTENT: 1. Create project realm (already idempotent - handles 409)
        await keycloak.create_realm(realm_name=realm_name, display_name=f"{project_name} ({cluster})")
        logger.info(f"Ensured realm {realm_name} exists")

        # IDEMPOTENT: 2. Create admin user in master realm (already idempotent - handles 409)
        user_info = await keycloak.create_user(
            realm_name="master", username=admin_username, password=admin_password, enabled=True
        )
        logger.info(f"Ensured admin user {admin_username} exists in master realm")

        # IDEMPOTENT: 3. Assign realm management roles (idempotent - assigns all available roles)
        await keycloak.assign_realm_management_role(
            realm_name="master", user_id=user_info["id"], target_realm=realm_name
        )
        logger.info(f"Ensured realm management roles assigned to {admin_username} for {realm_name}")

        # IDEMPOTENT: 4. Create project client in RIG Platform realm (already idempotent - handles 409)
        redirect_uri = f"{keycloak_url}/realms/{realm_name}/broker/rig-platform-oidc/endpoint"

        platform_client_info = await keycloak.create_federation_client(
            client_id=platform_client_id,
            redirect_uris=[redirect_uri],
            realm_name=settings.KEYCLOAK_DEFAULT_REALM,
        )
        logger.info(f"Ensured platform client {platform_client_id} exists in RIG Platform realm")

        # IDEMPOTENT: 5. Add RIG Platform as IDP in project realm (already idempotent - handles 409)
        platform_discovery_url = (
            f"{keycloak_url}/realms/{settings.KEYCLOAK_DEFAULT_REALM}/.well-known/openid-configuration"
        )

        await keycloak.add_identity_provider(
            realm_name=realm_name,
            provider_alias="rig-platform-oidc",
            display_name="RIG Platform",
            client_id=platform_client_info["client_id"],
            client_secret=platform_client_info["client_secret"],
            discovery_url=platform_discovery_url,
        )
        logger.info(f"Ensured RIG Platform IDP exists in realm {realm_name}")

        # IDEMPOTENT: 5b. Ensure IDP mappers exist
        await keycloak.ensure_standard_oidc_mappers(realm_name, "rig-platform-oidc")
        logger.info(f"Ensured IDP mappers for RIG Platform in realm {realm_name}")

        # IDEMPOTENT: 6. Configure SSO-only authentication flow (should be idempotent)
        await keycloak.configure_sso_redirect_flow(realm_name, "rig-platform-oidc")
        logger.info(f"Ensured SSO-only authentication configured for realm {realm_name}")

        # IDEMPOTENT: 7. Create custom_attributes_passthrough client scope (idempotent)
        await keycloak.create_custom_client_scope(
            realm_name=realm_name, scope_name="custom_attributes_passthrough"
        )
        logger.info(f"Ensured custom_attributes_passthrough client scope exists in realm {realm_name}")

        # IDEMPOTENT: 8. Store in project config (don't duplicate)
        if "config" not in project_data:
            project_data["config"] = {}
        if "keycloak" not in project_data["config"]:
            project_data["config"]["keycloak"] = []

        # Check if this realm config already exists
        existing_config = None
        for idx, kc_entry in enumerate(project_data["config"]["keycloak"]):
            if kc_entry.get("realm") == realm_name:
                existing_config = idx
                break

        config_entry = {
            "host": keycloak_url,
            "realm": realm_name,
            "username": admin_username,
            "password": encrypted_password_str,
        }

        if existing_config is not None:
            # Update existing entry
            project_data["config"]["keycloak"][existing_config] = config_entry
            logger.info(f"Updated existing Keycloak config for realm {realm_name}")
        else:
            # Add new entry
            project_data["config"]["keycloak"].append(config_entry)
            logger.info(f"Added new Keycloak config for realm {realm_name}")

        await self.save_project_data()
        logger.info(f"Stored Keycloak config in project file for cluster {cluster}")

        return {
            "host": keycloak_url,
            "realm": realm_name,
            "username": admin_username,
            "password": encrypted_password,
        }

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
                logger.error(f"Failed to delete realm {realm_name}: {e}")
                deletion_results["errors"].append(f"Realm deletion: {e}")

            # 2. Delete project admin from master realm
            try:
                await keycloak.delete_user_by_username("master", admin_username)
                logger.info(f"Deleted project admin {admin_username}")
                deletion_results["operations"].append(
                    {"type": "keycloak_user_deletion", "target": admin_username, "status": "success"}
                )
            except Exception as e:
                logger.error(f"Failed to delete user {admin_username}: {e}")
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
                logger.error(f"Failed to delete platform client: {e}")
                deletion_results["errors"].append(f"Platform client deletion: {e}")

            # 4. Remove keycloak config entry from project.yaml
            try:
                project_data = await self.get_contents()
                keycloak_list = project_data.get("config", {}).get("keycloak", [])

                # Remove entry matching this realm
                updated_list = [kc for kc in keycloak_list if kc.get("realm") != realm_name]

                if updated_list != keycloak_list:
                    project_data["config"]["keycloak"] = updated_list
                    await self.save_project_data()
                    logger.info(f"Removed keycloak config for realm {realm_name} from project.yaml")
                    deletion_results["operations"].append(
                        {
                            "type": "project_config_update",
                            "target": f"config.keycloak[{realm_name}]",
                            "status": "success",
                        }
                    )
            except Exception as e:
                logger.error(f"Failed to update project config: {e}")
                deletion_results["errors"].append(f"Config update: {e}")

        except Exception as e:
            logger.exception(f"Error during realm cleanup: {e}")
            deletion_results["errors"].append(f"Realm cleanup: {e}")

    async def _setup_sso_rijk_integration(
        self,
        project_name: str,
        component_name: str,
        deployment_name: str,
        namespace: str,
        hostname: str,
        ingress_hosts: list[str],
    ) -> dict[str, Any] | None:
        """
        Set up SSO-Rijk integration by adding a client to the project realm.

        This method now uses project-specific realms instead of the shared realm.
        It will create the project realm infrastructure if it doesn't exist yet.

        Args:
            project_name: Name of the project
            component_name: Name of the component
            deployment_name: Name of the deployment
            namespace: Kubernetes namespace
            hostname: Pre-calculated hostname for the deployment (primary)
            ingress_hosts: List of all ingress hostnames for this deployment

        Returns:
            SSO configuration dictionary or None if setup failed
        """
        try:
            # Get project data to find deployment cluster
            project_data = await self.get_contents()

            # Find deployment to get cluster
            deployments = project_data.get("deployments", [])
            deployment_config = None
            for d in deployments:
                if d.get("name") == deployment_name:
                    deployment_config = d
                    break

            if not deployment_config:
                raise Exception(f"Deployment {deployment_name} not found in project")

            cluster = deployment_config.get("cluster")
            if not cluster:
                raise Exception(f"Cluster not specified for deployment {deployment_name}")

            # Idempotent realm setup: Execute each step independently
            keycloak_url = self._get_keycloak_url_for_cluster(cluster)
            kc_config = self._get_project_keycloak_config_for_cluster(project_data, cluster)

            from opi.connectors.keycloak import create_keycloak_connector
            from opi.utils.naming import (
                generate_project_admin_username,
                generate_project_platform_client_id,
                generate_project_realm_name,
            )

            # Generate names
            admin_username = generate_project_admin_username(project_name, cluster)
            realm_name = generate_project_realm_name(project_name, cluster)
            platform_client_id = generate_project_platform_client_id(project_name, cluster)

            keycloak = await create_keycloak_connector(
                keycloak_url=keycloak_url,
                admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
            )

            # STEP 1: Ensure project realm exists
            if await keycloak.realm_exists(realm_name):
                logger.info(f"Project realm {realm_name} already exists")
            else:
                logger.info(f"Creating project realm {realm_name}")
                await keycloak.create_realm(realm_name=realm_name, display_name=f"{project_name} ({cluster})")

            # STEP 2: Ensure project admin user exists
            admin_user = await keycloak.get_user_by_username("master", admin_username)
            if not admin_user:
                logger.info(f"Creating project admin user {admin_username}")
                from opi.utils.passwords import generate_secure_password

                admin_password = generate_secure_password()
                admin_user = await keycloak.create_user(
                    realm_name="master", username=admin_username, password=admin_password, enabled=True
                )
            else:
                logger.info(f"Project admin user {admin_username} already exists")

            # STEP 3: Ensure admin has realm management roles
            await keycloak.assign_realm_management_role(
                realm_name="master", user_id=admin_user["id"], target_realm=realm_name
            )
            logger.info(f"Ensured realm management roles for {admin_username}")

            # STEP 4: Ensure federation client exists in RIG Platform realm
            redirect_uri = f"{keycloak_url}/realms/{realm_name}/broker/rig-platform-oidc/endpoint"
            platform_client_info = await keycloak.create_federation_client(
                client_id=platform_client_id,
                redirect_uris=[redirect_uri],
                realm_name=settings.KEYCLOAK_DEFAULT_REALM,
            )
            logger.info(f"Ensured federation client {platform_client_id} exists in RIG Platform realm")

            # STEP 5: Ensure RIG Platform IDP exists in project realm
            platform_discovery_url = (
                f"{keycloak_url}/realms/{settings.KEYCLOAK_DEFAULT_REALM}/.well-known/openid-configuration"
            )
            await keycloak.add_identity_provider(
                realm_name=realm_name,
                provider_alias="rig-platform-oidc",
                display_name="RIG Platform",
                client_id=platform_client_info["client_id"],
                client_secret=platform_client_info["client_secret"],
                discovery_url=platform_discovery_url,
            )
            logger.info(f"Ensured RIG Platform IDP exists in project realm {realm_name}")

            # STEP 5b: Ensure IDP mappers exist
            await keycloak.ensure_standard_oidc_mappers(realm_name, "rig-platform-oidc")
            logger.info(f"Ensured IDP mappers for RIG Platform in project realm {realm_name}")

            # STEP 6: Configure SSO-only authentication flow
            await keycloak.configure_sso_redirect_flow(realm_name, "rig-platform-oidc")
            logger.info(f"Ensured SSO-only authentication flow in realm {realm_name}")

            # STEP 7: Ensure custom client scope exists
            scopes = await keycloak.get_client_scopes(realm_name)
            scope_exists = any(scope.get("name") == "custom_attributes_passthrough" for scope in scopes)
            if not scope_exists:
                logger.info(f"Creating custom_attributes_passthrough scope in realm {realm_name}")
                await keycloak.create_custom_client_scope(
                    realm_name=realm_name, scope_name="custom_attributes_passthrough"
                )
            else:
                logger.info(f"Custom client scope already exists in realm {realm_name}")

            # STEP 8: Ensure config is stored in project file
            if not kc_config:
                logger.info(f"Storing project realm config for cluster {cluster}")
                from ruamel.yaml.scalarstring import LiteralScalarString

                from opi.utils.age import encrypt_age_content, get_project_public_key
                from opi.utils.passwords import generate_secure_password

                admin_password = generate_secure_password()
                project_public_key = get_project_public_key(project_data)
                encrypted_password = await encrypt_age_content(admin_password, project_public_key)

                if "config" not in project_data:
                    project_data["config"] = {}
                if "keycloak" not in project_data["config"]:
                    project_data["config"]["keycloak"] = []

                project_data["config"]["keycloak"].append(
                    {
                        "host": keycloak_url,
                        "realm": realm_name,
                        "username": admin_username,
                        "password": LiteralScalarString(encrypted_password),
                    }
                )
                await self.save_project_data()
                kc_config = self._get_project_keycloak_config_for_cluster(project_data, cluster)
            else:
                logger.info(f"Project realm config already exists for cluster {cluster}")

            keycloak_host = kc_config["host"]
            logger.info(f"Using project realm {realm_name} for deployment {deployment_name}")

            # Check if we have existing Keycloak credentials in secrets map
            existing_keycloak_secret = self._get_secret_from_map(deployment_name, "keycloak", KeycloakSecret)

            if existing_keycloak_secret:
                # Use existing credentials
                logger.info(f"Using existing Keycloak credentials for {component_name}")

                sso_config = {
                    "realm": {"name": realm_name},
                    "oidc": {
                        "client_id": existing_keycloak_secret.client_id,
                        "client_secret": existing_keycloak_secret.client_secret,
                        "discovery_url": existing_keycloak_secret.discovery_url,
                    },
                }
                return sso_config

            # No existing credentials found, create new Keycloak client
            logger.info(f"No existing Keycloak credentials found, creating new client for {component_name}")

            # Create Keycloak connector using project's Keycloak host
            keycloak_connector = await create_keycloak_connector(
                keycloak_url=keycloak_host,
                admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
            )

            # Use provided ingress hosts for Keycloak client configuration
            logger.info(f"Keycloak client will be configured with ingress hosts: {ingress_hosts}")

            # Create deployment client in the PROJECT REALM (not shared realm)
            client_info = await keycloak_connector.create_deployment_client(
                deployment_name=deployment_name,
                project_name=project_name,
                ingress_hosts=ingress_hosts,
                realm_name=realm_name,
            )

            # Encrypt the client secret using project's SOPS public key
            # Use project's AGE public key for encryption (not private key!)
            project_public_key = get_project_public_key(project_data)
            if project_public_key:
                encrypted_secret = await encrypt_age_content(client_info["client_secret"], project_public_key)
                encrypted_client_secret = LiteralScalarString(encrypted_secret)
                logger.info(
                    f"Successfully encrypted client secret for {client_info['client_id']} using project public key"
                )
            else:
                raise Exception(f"Project public key not found in project config for {project_name}")

            # Generate cluster-specific discovery URL for pods
            cluster_keycloak_discovery_url = get_keycloak_discovery_url(settings.CLUSTER_MANAGER)
            realm_name = client_info["realm"]
            cluster_discovery_url = (
                f"{cluster_keycloak_discovery_url}/realms/{realm_name}/.well-known/openid-configuration"
            )

            # Update client_info with cluster-specific discovery URL before storing
            client_info["discovery_url"] = cluster_discovery_url

            # Store credentials in private secrets map instead of project config
            keycloak_secret = KeycloakSecret(
                client_id=client_info["client_id"],
                client_secret=client_info["client_secret"],
                discovery_url=cluster_discovery_url,
            )
            self._add_secret_to_create(deployment_name, "keycloak", keycloak_secret)

            # Transform the response to match the expected sso_config format
            sso_config = {
                "realm": {"name": realm_name},
                "oidc": {
                    "client_id": client_info["client_id"],
                    "client_secret": client_info["client_secret"],
                    "encrypted_client_secret": encrypted_client_secret,
                    "discovery_url": cluster_discovery_url,  # Use cluster-specific URL for pods
                },
            }

            logger.info(f"SSO client created in shared realm for {component_name}")
            return sso_config

        except Exception as e:
            logger.exception(f"Failed to setup SSO-Rijk integration for {component_name}: {e}")
            return None

    def _generate_config_hash(
        self,
        env_vars: dict[str, Any],
        env_from_secrets: list[str],
        user_env_vars: dict[str, Any],
        secret_pairs: dict[str, Any],
    ) -> str:
        """
        Generate a hash of configuration data that should trigger deployment reload when changed.

        This includes:
        - Direct environment variables
        - List of secrets referenced via envFrom
        - User environment variables
        - Secret values from secret_pairs (actual secret content)

        Args:
            env_vars: Direct environment variables
            env_from_secrets: List of secret names used in envFrom
            user_env_vars: User-defined environment variables
            secret_pairs: Secret key-value pairs with actual secret content

        Returns:
            SHA256 hash of the configuration data
        """
        import hashlib
        import json

        # Create a stable representation of all configuration data
        config_data = {
            "env_vars": sorted(env_vars.items()) if env_vars else [],
            "env_from_secrets": sorted(env_from_secrets) if env_from_secrets else [],
            "user_env_vars": sorted(user_env_vars.items()) if user_env_vars else [],
            "secret_pairs": sorted(secret_pairs.items()) if secret_pairs else [],
        }

        # Convert to JSON string with sorted keys for consistent hashing
        config_json = json.dumps(config_data, sort_keys=True, separators=(",", ":"))

        # Generate SHA256 hash
        config_hash = hashlib.sha256(config_json.encode("utf-8")).hexdigest()[:16]  # Use first 16 chars

        logger.debug(
            f"Generated config hash: {config_hash} from {len(config_json)} chars of config data (including secret values)"
        )
        return config_hash

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
            project_data = await self.get_contents()

            # Find deployment config
            deployments = project_data.get("deployments", [])
            deployment_config = None
            for d in deployments:
                if d.get("name") == deployment_name:
                    deployment_config = d
                    break

            if not deployment_config:
                logger.warning(f"Deployment {deployment_name} not found in project {project_name}")
                deletion_results["errors"].append(f"Deployment {deployment_name} not found")
                return deletion_results

            cluster = deployment_config.get("cluster")

            # Get keycloak config for cluster
            kc_config = self._get_project_keycloak_config_for_cluster(project_data, cluster)

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
                    logger.error(f"Failed to delete Keycloak client: {e}")
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
            logger.exception(f"Error deleting deployment {deployment_name}: {e}")
            deletion_results["success"] = False
            deletion_results["errors"].append(str(e))

        return deletion_results

    async def delete_project_resources(self, project_name: str) -> dict[str, Any]:
        """
        Delete all resources associated with a project.

        This function orchestrates the deletion of:
        1. Project YAML file from Git projects repository
        2. ArgoCD GitOps folders for all deployments/clusters
        3. Kubernetes namespaces for all deployments

        Args:
            project_name: Name of the project to delete

        Returns:
            Dictionary containing deletion results and status

        Raises:
            HTTPException: If critical operations fail
        """

        deletion_results = {"project": project_name, "operations": [], "success": True, "errors": []}

        git_connector = None
        gitops_connector = None

        try:
            # Step 1: Read project configuration to understand what needs to be deleted
            git_connector = GitConnector(
                repo_url=settings.GIT_PROJECTS_SERVER_URL,
                username=settings.GIT_PROJECTS_SERVER_USERNAME,
                password=settings.GIT_PROJECTS_SERVER_PASSWORD,
                branch=settings.GIT_PROJECTS_SERVER_BRANCH,
                repo_path=settings.GIT_PROJECTS_SERVER_REPO_PATH,
                project_name=project_name,  # Add project context for better error reporting
            )

            project_file_path = f"projects/{project_name}.yaml"
            project_content = await git_connector.read_file_content(project_file_path)
            if not project_content:
                raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

            # Parse project to get deployments, clusters, and repositories
            yaml = YAML()
            project_data = yaml.load(project_content)
            deployments = project_data.get("deployments", [])
            repositories = project_data.get("repositories", [])

            # Step 2: Delete manifest repository folders (from repositories configured in project.yaml)
            logger.info(
                f"Starting manifest repository deletion for project {project_name} with {len(repositories)} repositories"
            )

            # Group deployments by repository to avoid duplicate deletions
            deployments_by_repo = {}
            for deployment in deployments:
                repo_name = deployment.get("repository")
                cluster = deployment.get("cluster")
                if repo_name and cluster:
                    if repo_name not in deployments_by_repo:
                        deployments_by_repo[repo_name] = set()
                    deployments_by_repo[repo_name].add(cluster)

            # Delete from each manifest repository
            for repository in repositories:
                repo_name = repository.get("name")
                if repo_name not in deployments_by_repo:
                    logger.info(f"Repository {repo_name} not used in any deployments, skipping")
                    continue

                logger.info(f"Deleting from manifest repository: {repo_name}")

                try:
                    # Create git connector for this repository
                    repo_config = {
                        "url": repository.get("url"),
                        "branch": repository.get("branch", "main"),
                        "path": repository.get("path", "."),
                        "username": repository.get("username"),
                        "password": repository.get("password"),
                        "ssh_key_path": repository.get("ssh_key_path"),
                    }

                    manifest_connector = await self.get_git_connector_for_deployment(repo_name, repo_config)

                    # Delete cluster/project folders for each cluster that uses this repository
                    for cluster in deployments_by_repo[repo_name]:
                        # Manifest folder structure: {cluster}/{project_name}/
                        manifest_folder_path = f"{cluster}/{project_name}"
                        logger.info(f"Attempting to delete manifest folder: {manifest_folder_path}")

                        await manifest_connector.ensure_repo_cloned()
                        folder_full_path = os.path.join(manifest_connector.__working_dir, manifest_folder_path)
                        folder_exists = os.path.exists(folder_full_path)
                        logger.info(f"Manifest folder exists at {folder_full_path}: {folder_exists}")

                        if folder_exists:
                            # Delete directory using filesystem operations
                            shutil.rmtree(folder_full_path)
                            deletion_results["operations"].append(
                                {
                                    "type": "manifest_folder_deletion",
                                    "target": manifest_folder_path,
                                    "repository": repo_name,
                                    "cluster": cluster,
                                    "status": "success",
                                }
                            )
                            logger.info(f"Successfully deleted manifest folder: {manifest_folder_path}")
                        else:
                            deletion_results["operations"].append(
                                {
                                    "type": "manifest_folder_deletion",
                                    "target": manifest_folder_path,
                                    "repository": repo_name,
                                    "cluster": cluster,
                                    "status": "not_found",
                                }
                            )

                    # Commit changes to manifest repository
                    commit_message = f"Delete project '{project_name}' - removed manifest folders"
                    commit_result = await manifest_connector.commit_and_push_changes(commit_message)
                    if commit_result:
                        deletion_results["operations"].append(
                            {
                                "type": "manifest_repo_commit",
                                "repository": repo_name,
                                "status": "success",
                                "message": commit_message,
                            }
                        )
                        logger.info(f"Successfully committed manifest deletions to {repo_name}")
                    else:
                        deletion_results["operations"].append(
                            {
                                "type": "manifest_repo_commit",
                                "repository": repo_name,
                                "status": "failed",
                                "error": "Failed to commit manifest changes",
                            }
                        )
                        deletion_results["errors"].append(f"Failed to commit manifest changes to {repo_name}")

                    # Clean up manifest connector
                    await manifest_connector.close()

                except Exception as e:
                    deletion_results["operations"].append(
                        {"type": "manifest_repo_deletion", "repository": repo_name, "status": "error", "error": str(e)}
                    )
                    deletion_results["errors"].append(f"Error deleting from manifest repository {repo_name}: {e}")
                    logger.exception(f"Error deleting from manifest repository {repo_name}: {e}")

            # Step 3: Delete GitOps repository folders (this will trigger ArgoCD app deletion via GitOps)
            logger.info(
                f"Starting GitOps folder deletion for project {project_name} with {len(deployments)} deployments"
            )

            try:
                # Create GitOps connector using the existing method
                # TODO: FIX ME DELETE IS BROKEN
                gitops_connector = await self.get_git_connector_for_deployment()
                logger.info(f"Successfully created GitOps connector for {project_name}")
            except Exception as e:
                logger.exception(f"Failed to create GitOps connector for {project_name}: {e}")
                deletion_results["errors"].append(f"Failed to create GitOps connector: {e}")
                # Continue with other deletion steps
                gitops_connector = None

            if gitops_connector:
                for deployment in deployments:
                    deployment_name = deployment.get("name")
                    cluster = deployment.get("cluster")

                    try:
                        # GitOps folder structure: clusters/{cluster}/{project}/{deployment}
                        gitops_folder_path = f"clusters/{cluster}/{project_name}/{deployment_name}"
                        logger.info(f"Attempting to delete GitOps folder: {gitops_folder_path}")

                        # Check if the folder exists and delete it using atomic git operations
                        await gitops_connector.ensure_repo_cloned()
                        folder_full_path = os.path.join(gitops_connector.working_dir, gitops_folder_path)
                        folder_exists = os.path.exists(folder_full_path)
                        logger.info(f"GitOps folder exists at {folder_full_path}: {folder_exists}")
                        if folder_exists:
                            # Use atomic operation: delete folder and stage deletion
                            await gitops_connector.delete_folder(gitops_folder_path)
                            delete_result = True
                            if delete_result:
                                deletion_results["operations"].append(
                                    {
                                        "type": "gitops_folder_deletion",
                                        "target": gitops_folder_path,
                                        "cluster": cluster,
                                        "deployment": deployment_name,
                                        "status": "success",
                                    }
                                )
                                logger.info(f"Successfully deleted GitOps folder: {gitops_folder_path}")
                            else:
                                deletion_results["operations"].append(
                                    {
                                        "type": "gitops_folder_deletion",
                                        "target": gitops_folder_path,
                                        "cluster": cluster,
                                        "deployment": deployment_name,
                                        "status": "failed",
                                        "error": "Git delete operation failed",
                                    }
                                )
                                deletion_results["errors"].append(
                                    f"Failed to delete GitOps folder {gitops_folder_path}"
                                )
                        else:
                            deletion_results["operations"].append(
                                {
                                    "type": "gitops_folder_deletion",
                                    "target": gitops_folder_path,
                                    "cluster": cluster,
                                    "deployment": deployment_name,
                                    "status": "not_found",
                                }
                            )

                    except Exception as e:
                        deletion_results["operations"].append(
                            {
                                "type": "gitops_folder_deletion",
                                "target": f"clusters/{cluster}/{project_name}/{deployment_name}",
                                "cluster": cluster,
                                "deployment": deployment_name,
                                "status": "error",
                                "error": str(e),
                            }
                        )
                        deletion_results["errors"].append(f"Error deleting GitOps folder for {deployment_name}: {e}")
                        logger.exception(f"Error deleting GitOps folder for {deployment_name}: {e}")

            # Step 3.5: Delete Keycloak clients and realms
            logger.info(f"Starting Keycloak cleanup for project {project_name} with {len(deployments)} deployments")

            # Group deployments by cluster to handle realm cleanup per cluster
            deployments_by_cluster = {}
            for deployment in deployments:
                cluster = deployment.get("cluster")
                if cluster:
                    if cluster not in deployments_by_cluster:
                        deployments_by_cluster[cluster] = []
                    deployments_by_cluster[cluster].append(deployment)

            # Delete clients per cluster and cleanup realms
            for cluster, cluster_deployments in deployments_by_cluster.items():
                try:
                    # Get keycloak config for this cluster
                    kc_config = self._get_project_keycloak_config_for_cluster(project_data, cluster)

                    if kc_config:
                        realm_name = kc_config["realm"]
                        keycloak_host = kc_config["host"]

                        logger.info(
                            f"Cleaning up {len(cluster_deployments)} deployment clients from realm {realm_name}"
                        )

                        keycloak = await create_keycloak_connector(
                            keycloak_url=keycloak_host,
                            admin_username=settings.KEYCLOAK_ADMIN_USERNAME,
                            admin_password=settings.KEYCLOAK_ADMIN_PASSWORD,
                        )

                        # Delete deployment clients from project realm
                        for deployment in cluster_deployments:
                            deployment_name = deployment.get("name")

                            try:
                                logger.info(
                                    f"Deleting Keycloak client for deployment {deployment_name} from realm {realm_name}"
                                )

                                delete_success = await keycloak.delete_deployment_client(
                                    deployment_name=deployment_name, project_name=project_name, realm_name=realm_name
                                )

                                if delete_success:
                                    deletion_results["operations"].append(
                                        {
                                            "type": "keycloak_client_deletion",
                                            "target": f"{project_name}-{deployment_name}",
                                            "realm": realm_name,
                                            "deployment": deployment_name,
                                            "status": "success",
                                        }
                                    )
                                    logger.info(
                                        f"Successfully deleted Keycloak client for deployment {deployment_name}"
                                    )
                                else:
                                    deletion_results["operations"].append(
                                        {
                                            "type": "keycloak_client_deletion",
                                            "target": f"{project_name}-{deployment_name}",
                                            "realm": realm_name,
                                            "deployment": deployment_name,
                                            "status": "not_found",
                                        }
                                    )

                            except Exception as e:
                                deletion_results["errors"].append(
                                    f"Error deleting Keycloak client for {deployment_name}: {e}"
                                )
                                logger.exception(f"Error deleting Keycloak client for {deployment_name}: {e}")

                        # After deleting all clients in this cluster, cleanup the realm
                        logger.info(f"Cleaning up project realm {realm_name} for cluster {cluster}")
                        await self._cleanup_project_keycloak_realm(
                            project_name=project_name,
                            cluster=cluster,
                            kc_config=kc_config,
                            deletion_results=deletion_results,
                        )

                    else:
                        # No realm config - might be old deployment using default realm
                        logger.info(
                            f"No project realm found for cluster {cluster}, trying default realm for backwards compatibility"
                        )

                        keycloak = await create_keycloak_connector()

                        for deployment in cluster_deployments:
                            deployment_name = deployment.get("name")

                            try:
                                delete_success = await keycloak.delete_deployment_client(
                                    deployment_name=deployment_name,
                                    project_name=project_name,
                                    realm_name=settings.KEYCLOAK_DEFAULT_REALM,
                                )

                                if delete_success:
                                    deletion_results["operations"].append(
                                        {
                                            "type": "keycloak_client_deletion",
                                            "target": f"{project_name}-{deployment_name}",
                                            "deployment": deployment_name,
                                            "status": "success",
                                        }
                                    )

                            except Exception as e:
                                logger.warning(f"Failed to delete legacy client for {deployment_name}: {e}")

                except Exception as e:
                    logger.error(f"Error during Keycloak cleanup for cluster {cluster}: {e}")
                    deletion_results["errors"].append(f"Keycloak cleanup for cluster {cluster}: {e}")

            # Step 4: Delete project YAML file from projects repository
            try:
                # Delete file using filesystem operations
                await git_connector.ensure_repo_cloned()
                file_full_path = os.path.join(await git_connector.get_working_dir(), project_file_path)
                if os.path.exists(file_full_path):
                    os.remove(file_full_path)
                    delete_result = True
                else:
                    delete_result = False
                if delete_result:
                    deletion_results["operations"].append(
                        {"type": "project_file_deletion", "target": project_file_path, "status": "success"}
                    )
                    logger.info(f"Successfully deleted project file: {project_file_path}")
                else:
                    deletion_results["operations"].append(
                        {
                            "type": "project_file_deletion",
                            "target": project_file_path,
                            "status": "failed",
                            "error": "Git delete operation failed",
                        }
                    )
                    deletion_results["errors"].append(f"Failed to delete project file {project_file_path}")

            except Exception as e:
                deletion_results["operations"].append(
                    {"type": "project_file_deletion", "target": project_file_path, "status": "error", "error": str(e)}
                )
                deletion_results["errors"].append(f"Error deleting project file: {e}")
                logger.exception(f"Error deleting project file: {e}")

            # Step 5: Commit changes to GitOps repository
            successful_gitops_deletions = [
                op
                for op in deletion_results["operations"]
                if op["type"] == "gitops_folder_deletion" and op["status"] == "success"
            ]
            logger.info(f"Found {len(successful_gitops_deletions)} successful GitOps folder deletions")

            if successful_gitops_deletions:
                try:
                    commit_message = f"Delete project '{project_name}' - removed GitOps resources"
                    # Use atomic operation: commit and push all staged deletions
                    await gitops_connector.commit_and_push(commit_message)

                    deletion_results["operations"].append(
                        {"type": "gitops_commit", "status": "success", "message": commit_message}
                    )

                    # Refresh user-applications to make ArgoCD aware of the deleted resources
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

                        # Step 5.5: Wait for ArgoCD applications to be deleted (triggered by GitOps manifest removal)
                        # This should happen regardless of refresh success/failure
                        try:
                            for deployment in deployments:
                                deployment_name = deployment.get("name")
                                cluster = deployment.get("cluster")
                                app_name = generate_argocd_application_name(project_name, deployment_name)

                                try:
                                    # Check if application exists and wait for it to be deleted
                                    app_exists = await argo_connector.application_exists(app_name)
                                    if app_exists:
                                        logger.info(
                                            f"Waiting for ArgoCD application {app_name} to be deleted via GitOps"
                                        )
                                        deletion_complete = await argo_connector.wait_for_application_deletion(
                                            app_name, max_retries=5
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
                                            logger.info(
                                                f"ArgoCD application {app_name} successfully deleted via GitOps"
                                            )
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
                                            logger.warning(
                                                f"ArgoCD application {app_name} deletion timed out - continuing anyway"
                                            )
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
                                        logger.info(
                                            f"ArgoCD application {app_name} was not found (already deleted or never existed)"
                                        )
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
                                    logger.exception(f"Error monitoring ArgoCD application deletion: {e}")
                        except Exception as argo_error:
                            deletion_results["errors"].append(
                                f"Error creating ArgoCD connector for monitoring: {argo_error}"
                            )
                            logger.exception(f"Error creating ArgoCD connector for monitoring: {argo_error}")
                    else:
                        deletion_results["operations"].append(
                            {"type": "gitops_commit", "status": "failed", "error": "Failed to commit GitOps changes"}
                        )
                        deletion_results["errors"].append("Failed to commit GitOps changes")
                except Exception as e:
                    deletion_results["operations"].append({"type": "gitops_commit", "status": "error", "error": str(e)})
                    deletion_results["errors"].append(f"Error committing GitOps changes: {e}")

            # Step 6: Commit changes to projects repository
            try:
                commit_message = f"Delete project '{project_name}'"
                commit_result = await git_connector.commit_and_push_changes(commit_message)
                if commit_result:
                    deletion_results["operations"].append(
                        {"type": "project_commit", "status": "success", "message": commit_message}
                    )
                else:
                    deletion_results["operations"].append(
                        {"type": "project_commit", "status": "failed", "error": "Failed to commit project changes"}
                    )
                    deletion_results["errors"].append("Failed to commit project changes")
            except Exception as e:
                deletion_results["operations"].append({"type": "project_commit", "status": "error", "error": str(e)})
                deletion_results["errors"].append(f"Error committing project changes: {e}")

            # Step 7: Delete Kubernetes namespaces LAST (after ArgoCD applications are removed to avoid finalizer issues)
            for deployment in deployments:
                deployment_name = deployment.get("name")
                cluster = deployment.get("cluster")
                base_namespace = deployment.get("namespace", project_name)
                namespace = get_prefixed_namespace(cluster, base_namespace)

                try:
                    # Delete namespace if it exists
                    namespace_exists = await self._kubectl_connector.namespace_exists(namespace)
                    if namespace_exists:
                        # Use kubectl command to delete namespace
                        _, _, returncode = await self._kubectl_connector._run_kubectl_command(
                            ["delete", "namespace", namespace, "--ignore-not-found=true"]
                        )
                        delete_result = returncode == 0
                        if delete_result:
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
                                    "status": "failed",
                                    "error": "Kubectl delete operation failed",
                                }
                            )
                            deletion_results["errors"].append(f"Failed to delete namespace {namespace}")
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
                    logger.exception(f"Error deleting namespace {namespace}: {e}")

            # Determine overall success
            deletion_results["success"] = len(deletion_results["errors"]) == 0

            return deletion_results

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Critical error during project deletion for {project_name}: {e}")
            raise HTTPException(status_code=500, detail=f"Critical error during project deletion: {e!s}")
        finally:
            # Clean up GitConnectors
            if git_connector:
                try:
                    await git_connector.close()
                except Exception as e:
                    logger.exception(f"Error closing git connector: {e}")
            if gitops_connector:
                try:
                    await gitops_connector.close()
                except Exception as e:
                    logger.exception(f"Error closing gitops connector: {e}")

    async def delete_project_with_deployment_cleanup(self, project_name: str) -> dict[str, Any]:
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

        # TODO: maybe this should be done differently!!!
        self._project_file_relative_path = f"projects/{project_name}.yaml"

        try:
            # Step 1: Read project configuration to understand what deployments exist
            # Use get_contents() method to read project data
            project_data = await self.get_contents()
            deployments = project_data.get("deployments", [])

            # Step 2: Separate deployments by cluster
            current_cluster = settings.CLUSTER_MANAGER
            current_cluster_deployments = []
            other_cluster_deployments = []

            for deployment in deployments:
                deployment_cluster = deployment.get("cluster")
                if deployment_cluster == current_cluster:
                    current_cluster_deployments.append(deployment)
                else:
                    other_cluster_deployments.append(deployment)

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

            # Step 5: Only delete the project file if all deployment deletions succeeded
            if deletion_results["success"] and len(current_cluster_deployments) > 0:
                logger.info(f"All deployments deleted successfully, now deleting project file for {project_name}")

                commit_message = f"Delete project '{project_name}' - removed project file after deployment cleanup"
                delete_result = await self._delete_project_file(project_name, commit_message)

                deletion_results["operations"].extend(delete_result["operations"])
                deletion_results["errors"].extend(delete_result["errors"])
                if not delete_result["success"]:
                    deletion_results["success"] = False

            elif len(current_cluster_deployments) == 0:
                # No deployments on current cluster, but we still need to check if project should be deleted
                logger.info(f"No deployments found on current cluster '{current_cluster}' for project {project_name}")
                deletion_results["operations"].append(
                    {
                        "type": "project_status_check",
                        "status": "no_deployments_on_current_cluster",
                        "message": f"Project has no deployments on current cluster '{current_cluster}'",
                    }
                )

                # Since we already checked other clusters above and would have returned if any existed,
                # we can safely delete the project file
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
                    # Don't mark overall deletion as failed since this is a cleanup step
                    logger.warning(error_msg)

            return deletion_results

        except HTTPException:
            raise
        except Exception as e:
            deletion_results["success"] = False
            deletion_results["errors"].append(f"Unexpected error during project deletion: {e}")
            logger.exception(f"Unexpected error during project deletion for {project_name}: {e}")
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
            git_connector = await self.get_git_connector_for_project_files()
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

        # TODO: maybe this should be done differently
        self._project_file_relative_path = f"projects/{project_name}.yaml"

        try:
            # Step 1: Read project configuration to understand what needs to be deleted
            git_connector = await self.get_git_connector_for_project_files()
            await git_connector.ensure_repo_cloned()

            project_data = await self.get_contents()

            # Find the specific deployment
            deployment = None
            for dep in project_data.get("deployments", []):
                if dep.get("name") == deployment_name:
                    deployment = dep
                    break

            if not deployment:
                raise HTTPException(
                    status_code=404, detail=f"Deployment '{deployment_name}' not found in project '{project_name}'"
                )

            logger.info(f"Starting deployment deletion for {project_name}/{deployment_name}")

            # Step 2: Delete service resources (bottom-up approach)
            logger.info(f"Deleting service resources for {project_name}/{deployment_name}")

            # Delete Keycloak resources
            keycloak_results = await self._keycloak_manager.delete_resources_for_deployment(project_data, deployment)
            deletion_results["service_results"]["keycloak"] = keycloak_results
            deletion_results["operations"].extend(keycloak_results["operations"])
            if keycloak_results["errors"]:
                deletion_results["errors"].extend(keycloak_results["errors"])

            # Delete database resources
            database_results = await self._database_manager.delete_resources_for_deployment(project_data, deployment)
            deletion_results["service_results"]["database"] = database_results
            deletion_results["operations"].extend(database_results["operations"])
            if database_results["errors"]:
                deletion_results["errors"].extend(database_results["errors"])

            # Delete MinIO resources
            minio_results = await self._minio_manager.delete_resources_for_deployment(project_data, deployment)
            deletion_results["service_results"]["minio"] = minio_results
            deletion_results["operations"].extend(minio_results["operations"])
            if minio_results["errors"]:
                deletion_results["errors"].extend(minio_results["errors"])

            # Step 3: Delete deployment folders from git repositories
            logger.info(f"Deleting deployment manifests for {project_name}/{deployment_name}")

            repository_name = deployment.get("repository")
            cluster = deployment.get("cluster")

            if repository_name and cluster:
                try:
                    # Find repository configuration
                    repositories = project_data.get("repositories", [])
                    repo_config = None
                    for repo in repositories:
                        if repo.get("name") == repository_name:
                            repo_config = repo
                            break

                    if repo_config:
                        manifest_connector = await self.get_git_connector_for_deployment(repository_name, repo_config)

                        # Delete entire deployment folder using naming utility
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
                            # Delete entire deployment directory
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

                            # Commit changes to manifest repository
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
                    logger.exception(f"Error deleting deployment folder: {e}")

            # Step 4: Delete ArgoCD application file from GitOps
            logger.info(f"Deleting ArgoCD application file for {project_name}/{deployment_name}")

            try:
                gitops_connector = await self.get_git_connector_for_argocd()

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

                # Rebuild kustomization file (always, regardless of whether file existed)
                logger.info(f"Rebuilding kustomization.yaml for project {project_name} in cluster {cluster}")
                working_dir = await gitops_connector.get_working_dir()
                project_dir = os.path.join(working_dir, cluster, project_name)

                kustomization_success = self._manifest_generator.create_kustomization_files(
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
                logger.exception(f"Error deleting ArgoCD application file: {e}")

            # Step 4.1: Delete ArgoCD AppProject file (only if no other deployments use the same namespace)
            current_base_namespace = deployment.get("namespace")
            namespace_used_by_others = any(
                other_dep.get("name") != deployment_name
                and other_dep.get("cluster") == cluster
                and other_dep.get("namespace") == current_base_namespace
                for other_dep in project_data.get("deployments", [])
            )

            if not namespace_used_by_others:
                # Delete AppProject file
                appproject_name = generate_argocd_appproject_prefix(project_name, current_base_namespace)
                appproject_filename = get_output_filename_from_template("argocd-appproject.yaml.jinja", appproject_name)
                appproject_file_path = os.path.join(
                    await gitops_connector.get_working_dir(), cluster, project_name, appproject_filename
                )
                if os.path.exists(appproject_file_path):
                    os.remove(appproject_file_path)
                    logger.info(f"Deleted AppProject file: {appproject_filename}")

            # Step 4.2: Delete Repository Secret files (only if no other deployments use the same repository)
            current_repo = deployment.get("repository")
            if current_repo:
                repo_used_by_others = any(
                    other_dep.get("name") != deployment_name and other_dep.get("repository") == current_repo
                    for other_dep in project_data.get("deployments", [])
                )

                if not repo_used_by_others:
                    # Delete repository secret files
                    unique_repo_name = f"{project_name}-{current_repo}"
                    for template in ["argo-repository-https.yaml.jinja", "argo-repository.yaml.jinja"]:
                        filename = get_output_filename_from_template(template, unique_repo_name)
                        file_path = os.path.join(
                            await gitops_connector.get_working_dir(), cluster, project_name, filename
                        )
                        if os.path.exists(file_path):
                            os.remove(file_path)
                            logger.info(f"Deleted repository file: {filename}")

            # Step 4.5: Refresh user-applications to make ArgoCD aware of the deleted resources
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

            # Step 5: Wait for ArgoCD application deletion (triggered by GitOps manifest removal)
            try:
                from opi.utils.naming import generate_argocd_application_name

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
                logger.exception(f"Error monitoring ArgoCD application deletion: {e}")

            # Step 6: Delete Kubernetes namespace (only if no other deployments use the same namespace)
            try:
                base_namespace = deployment.get("namespace", project_name)
                namespace = get_prefixed_namespace(cluster, base_namespace)

                # Check if any other deployment uses the same namespace
                namespace_used_by_others = any(
                    other_dep.get("name") != deployment_name
                    and other_dep.get("cluster") == cluster
                    and other_dep.get("namespace") == base_namespace
                    for other_dep in project_data.get("deployments", [])
                )

                if not namespace_used_by_others:
                    logger.info(f"Deleting Kubernetes namespace: {namespace}")
                    namespace_deleted = await self._kubectl_connector.delete_namespace(namespace)

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
                logger.exception(f"Error deleting namespace {namespace}: {e}")

            # Step 7: Remove deployment from project file
            try:
                logger.info(f"Removing deployment '{deployment_name}' from project file for project '{project_name}'")

                # Get current project data
                current_project_data = await self.get_contents()

                # Remove the deployment by matching name
                updated_deployments = [
                    dep for dep in current_project_data.get("deployments", []) if dep.get("name") != deployment_name
                ]
                current_project_data["deployments"] = updated_deployments

                # Save the updated project data (cached data is already modified)
                await self.save_project_data()

                # Commit and push the changes
                git_connector = await self.get_git_connector_for_project_files()
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
                logger.exception(f"Error removing deployment '{deployment_name}' from project file: {e}")

            # Update success status based on errors
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
            logger.exception(f"Critical error during deployment deletion for {project_name}/{deployment_name}: {e}")
            raise HTTPException(status_code=500, detail=f"Critical error during deployment deletion: {e!s}")
        finally:
            await self.close()

    async def update_image(self, deployment_name: str, component_name: str, new_image_url: str) -> dict[str, Any]:
        """
        Validate, update component image, commit changes, and trigger project processing.

        Args:
            project_name: Name of the project
            component_name: Name of the component to update
            deployment_name: Name of the deployment to update
            new_image_url: New image URL to set

        Returns:
            Dictionary containing update results and status

        Raises:
            HTTPException: If validation fails or update fails
        """

        try:
            # Construct JSON path to the target image field
            json_path = (
                f"deployments[?(@.name=='{deployment_name}')].components[?(@.reference=='{component_name}')].image"
            )

            # Get current image value using JSON path
            current_image = await self.find_value_by_jsonpath(json_path)
            if current_image is None:
                raise HTTPException(
                    status_code=404, detail=f"Component '{component_name}' in deployment '{deployment_name}' not found"
                )

            # Check if image is actually changing
            if current_image == new_image_url:
                return {
                    "status": "no_change",
                    "message": f"Image is already set to '{new_image_url}'",
                    "current_image": current_image,
                    "new_image": new_image_url,
                }

            # Actually perform the update using the fast image update method
            update_success = await self.update_component_image_fast(component_name, deployment_name, new_image_url)

            if not update_success:
                raise HTTPException(status_code=500, detail=f"Failed to update image for component '{component_name}'")

            return {
                "status": "updated",
                "message": f"Successfully updated '{current_image}' to '{new_image_url}' and triggered deployment refresh",
                "current_image": current_image,
                "new_image": new_image_url,
                "json_path": json_path,
                "actions_performed": {
                    "yaml_updated": True,
                    "git_committed": True,
                    "deployment_manifest_updated": True,
                    "argocd_refreshed": True,
                },
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Error updating image for {project_name}: {e}")
            raise HTTPException(status_code=500, detail=f"Error updating image: {e!s}")

    async def update_component_image(
        self, project_name: str, component_name: str, deployment_name: str, new_image_url: str
    ) -> bool:
        """
        Update a component's image in a project and trigger processing.

        Args:
            project_name: Name of the project
            component_name: Name of the component to update
            deployment_name: Name of the deployment to update
            new_image_url: New image URL to set

        Returns:
            True if update was successful, False otherwise
        """
        try:
            # Construct JSON path to the target image field
            json_path = (
                f"deployments[?(@.name=='{deployment_name}')].components[?(@.reference=='{component_name}')].image"
            )

            # Update the project file
            commit_message = f"Update {component_name} image to {new_image_url} in deployment {deployment_name}"
            update_success = await self.update_project_field_by_path(
                project_name, json_path, new_image_url, commit_message
            )
            await self.save_project_data()
            await (await self.get_git_connector_for_project_files()).commit_and_push(
                f"Updated image for project {project_name} to {new_image_url}"
            )

            if not update_success:
                logger.error(f"Failed to update project file for {project_name}")
                return False

            # Trigger project processing to regenerate manifests and sync
            # Use the process_project_from_git method instead to handle Git operations properly
            project_file_path = f"projects/{project_name}.yaml"
            process_success = await self.process_project_from_git(project_file_path)

            if process_success:
                logger.info(f"Successfully updated and processed image change for {project_name}")
                return True
            else:
                logger.error(f"Failed to process project changes for {project_name}")
                return False

        except Exception as e:
            logger.exception(f"Error updating component image for {project_name}: {e}")
            return False

    async def update_component_image_fast(self, component_name: str, deployment_name: str, new_image_url: str) -> bool:
        """
        Fast update of a component's image in a project - only updates image URL in existing deployment manifest.

        This is an optimized version that:
        1. Updates the project file with new image URL
        2. Finds and updates the image URL in the existing deployment manifest file
        3. Commits and pushes the specific manifest change
        4. Refreshes only the relevant ArgoCD application

        Args:
            project_name: Name of the project
            component_name: Name of the component to update
            deployment_name: Name of the deployment to update
            new_image_url: New image URL to set

        Returns:
            True if update was successful, False otherwise
        """
        try:
            # Step 1: Update the project file with new image URL
            json_path = (
                f"deployments[?(@.name=='{deployment_name}')].components[?(@.reference=='{component_name}')].image"
            )

            project_name = await self.get_name()

            commit_message = f"Update {component_name} image to {new_image_url} in deployment {deployment_name}"
            update_success = await self.update_project_field_by_path(
                project_name, json_path, new_image_url, commit_message
            )

            await self.save_project_data()
            await (await self.get_git_connector_for_project_files()).commit_and_push(
                f"Updated image for project {project_name} to {new_image_url}"
            )

            if not update_success:
                logger.error(f"Failed to update project file for {project_name}")
                return False

            # Step 2: Get project data and validate deployment exists
            project_data = await self.get_contents()

            # Find the specific deployment for current cluster using JSONPath
            target_deployment = find_value_by_jsonpath(
                project_data, f"$.deployments[?(@.name=='{deployment_name}' & @.cluster=='{settings.CLUSTER_MANAGER}')]"
            )

            if not target_deployment:
                logger.error(f"Deployment '{deployment_name}' not found or not for current cluster")
                return False

            # Step 3: Get repository configuration for this deployment
            repository_name = target_deployment.get("repository")
            if not repository_name:
                logger.error(f"No repository specified for deployment '{deployment_name}'")
                return False

            repo_config = find_value_by_jsonpath(project_data, f"$.repositories[?(@.name=='{repository_name}')]")

            if not repo_config:
                logger.error(f"Repository configuration not found: {repository_name}")
                return False

            # Step 4: Update the image URL in the existing deployment manifest
            logger.info(
                f"Updating image URL in deployment manifest for {project_name}/{deployment_name}/{component_name}"
            )

            # Get git connector for the repository
            git_connector = await self.get_git_connector_for_deployment(repository_name, repo_config)
            await git_connector.ensure_repo_cloned()

            # Generate deployment path using naming utility
            cluster = target_deployment.get("cluster")
            repo_path = repo_config.get("path", "")
            deployment_path = generate_deployment_manifest_path(cluster, project_name, deployment_name, repo_path)

            # Update the deployment manifest with new image URL
            manifest_updated = await self._update_deployment_manifest_image(
                git_connector, deployment_path, component_name, new_image_url
            )

            if not manifest_updated:
                logger.error("Failed to update deployment manifest image URL")
                return False

            # Step 5: Commit and push only the deployment manifest change
            manifest_commit_message = f"Update deployment manifest image for {component_name}"
            commit_success = await git_connector.commit_and_push_changes(manifest_commit_message)

            if not commit_success:
                logger.error("Failed to commit deployment manifest changes")
                return False

            # Step 6: Refresh only the relevant ArgoCD application
            argo_connector = create_argo_connector()
            app_name = generate_argocd_application_name(project_name, deployment_name)

            if await argo_connector.application_exists(app_name):
                logger.info(f"Refreshing ArgoCD application: {app_name}")
                refresh_result = await argo_connector.refresh_application(app_name)
                if refresh_result:
                    logger.info(f"Successfully refreshed application: {app_name}")
                else:
                    logger.warning(f"Failed to refresh application: {app_name}")
                    # Don't fail the entire operation if refresh fails
            else:
                logger.warning(f"ArgoCD application {app_name} does not exist, skipping refresh")

            logger.info(f"Fast image update completed for {project_name}/{deployment_name}/{component_name}")
            return True

        except Exception as e:
            logger.exception(f"Error in fast image update for {project_name}: {e}")
            return False

    async def _update_deployment_manifest_image(
        self, git_connector: GitConnector, deployment_path: str, component_name: str, new_image_url: str
    ) -> bool:
        """
        Update the image URL in an existing deployment manifest file using YAML parsing.

        This method finds the deployment manifest file and updates the image URL
        without recreating the entire manifest, preserving all secrets and configuration.

        Args:
            git_connector: Git connector for the repository
            deployment_path: Path to the deployment directory
            component_name: Component name (used to identify the correct deployment file)
            new_image_url: New image URL to set

        Returns:
            True if update was successful, False otherwise
        """
        try:
            working_dir = await git_connector.get_working_dir()
            full_deployment_path = os.path.join(working_dir, deployment_path)

            # Use the naming utility to get the correct deployment filename
            deployment_filename = f"{generate_manifest_name(component_name, 'deployment')}.yaml"
            deployment_file_path = os.path.join(full_deployment_path, deployment_filename)

            if not os.path.exists(deployment_file_path):
                logger.error(f"Deployment manifest not found: {deployment_file_path}")
                return False

            # Load the existing deployment manifest
            deployment_data = load_yaml_from_path(deployment_file_path)
            if not deployment_data:
                logger.error(f"Failed to load deployment manifest: {deployment_file_path}")
                return False

            # Update the image URL using JSONPath
            json_path = "$.spec.template.spec.containers[0].image"
            old_image = find_value_by_jsonpath(deployment_data, json_path, "")

            update_success = update_value_by_jsonpath(deployment_data, json_path, new_image_url)
            if not update_success:
                logger.error(f"Failed to update image URL in deployment manifest: {deployment_file_path}")
                return False

            logger.info(f"Updated image from '{old_image}' to '{new_image_url}' in {deployment_filename}")

            # Save the updated deployment manifest
            save_success = save_yaml_to_path(deployment_file_path, deployment_data)

            if not save_success:
                logger.error("Failed to save updated deployment manifest")
                return False

            logger.info(f"Successfully updated image URL in deployment manifest: {deployment_file_path}")
            return True

        except Exception as e:
            logger.exception(f"Error updating deployment manifest image: {e}")
            return False

    async def add_deployment(
        self,
        deployment_name: str,
        components: list,  # ComponentReference objects from router
        clone_from: str | None = None,
    ) -> dict[str, Any]:
        """
        Add a new deployment to the project YAML file.

        Args:
            deployment_name: Name of the new deployment
            components: List of ComponentReference objects with reference and image
            clone_from: Optional deployment name to clone configuration from

        Returns:
            Dict with success status and error details if applicable:
            {"success": bool, "error": str | None, "error_type": str | None}
        """
        try:
            # Get current project data
            project_data = await self.get_contents()
            project_name = project_data.get("name")

            # Check if deployment already exists
            existing_deployments = project_data.get("deployments", [])
            for existing_deployment in existing_deployments:
                if existing_deployment.get("name") == deployment_name:
                    error_msg = f"Deployment '{deployment_name}' already exists in project '{project_name}'"
                    logger.error(error_msg)
                    return {"success": False, "error": error_msg, "error_type": "duplicate_deployment"}

            # Validate that all component references exist in the project
            validation_result = self._validate_component_references(project_data, components, "new deployment")
            if not validation_result["success"]:
                return {
                    "success": False,
                    "error": validation_result["error"],
                    "error_type": "invalid_component_references",
                }

            # Create new deployment object
            new_deployment = {"name": deployment_name, "components": []}

            # Convert components from router objects to dict format
            for component in components:
                new_deployment["components"].append({"reference": component.reference, "image": component.image})

            # Handle clone-from logic
            if clone_from:
                # Find source deployment to clone from
                source_deployment = find_value_by_jsonpath(project_data, f"$.deployments[?(@.name=='{clone_from}')]")

                if source_deployment:
                    logger.info(f"Cloning deployment configuration from '{clone_from}'")

                    # Clone all properties except name and components
                    for key, value in source_deployment.items():
                        if key not in ["name", "components"]:
                            new_deployment[key] = value

                    # If clone-from is specified, add force-clone flag
                    new_deployment["clone-from"] = clone_from
                else:
                    raise ValueError(f"Source deployment '{clone_from}' not found in project '{project_name}'")

            # Assume missing parameters from project configuration
            if not new_deployment.get("cluster"):
                # Use clusters from project root configuration
                project_clusters = project_data.get("clusters", [])
                if len(project_clusters) == 1:
                    new_deployment["cluster"] = project_clusters[0]
                elif len(project_clusters) > 1:
                    logger.error(
                        f"Multiple clusters defined in project '{project_name}': {project_clusters}. Cluster must be specified explicitly for new deployment."
                    )
                    return False

            if not new_deployment.get("namespace"):
                # TODO: make this a naming.py ? this is tricky..
                # Use project name as namespace (common pattern)
                new_deployment["namespace"] = project_name

            if not new_deployment.get("repository"):
                # Use repositories from project configuration
                repositories = project_data.get("repositories", [])
                if len(repositories) == 1:
                    new_deployment["repository"] = repositories[0]["name"]
                elif len(repositories) > 1:
                    repo_names = [repo["name"] for repo in repositories]
                    error_msg = f"Multiple repositories defined in project '{project_name}': {repo_names}. Repository must be specified explicitly for new deployment."
                    logger.error(error_msg)
                    return {"success": False, "error": error_msg, "error_type": "ambiguous_repository"}
                else:
                    error_msg = "No repositories found in project configuration"
                    logger.error(error_msg)
                    return {"success": False, "error": error_msg, "error_type": "no_repositories"}

            # Add the new deployment to the project data
            project_data["deployments"].append(new_deployment)

            # Save the updated project data
            await self.save_project_data()

            # Commit changes to Git
            git_connector = await self.get_git_connector_for_project_files()
            commit_message = f"Add deployment '{deployment_name}' to project '{project_name}'"
            if clone_from:
                commit_message += f" (cloned from '{clone_from}')"

            await git_connector.commit_and_push(commit_message)

            logger.info(f"Successfully added deployment '{deployment_name}' to project '{project_name}'")
            return {"success": True, "error": None, "error_type": None}

        except Exception as e:
            error_msg = f"Error adding deployment '{deployment_name}': {e}"
            logger.exception(error_msg)
            return {"success": False, "error": error_msg, "error_type": "internal_error"}

    def _validate_component_references(
        self, project_data: dict, components: list, context: str = "deployment"
    ) -> dict[str, Any]:
        """
        Validate that all component references exist in the project.

        Args:
            project_data: The project data containing component definitions
            components: List of ComponentReference objects or dicts with 'reference' key
            context: Context for error messages (e.g. "deployment", "update")

        Returns:
            Dict with validation result: {"success": bool, "error": str | None, "invalid_references": list | None}
        """
        project_components = project_data.get("components", [])
        component_names = {comp.get("name") for comp in project_components}
        invalid_references = []

        for component in components:
            # Handle both ComponentReference objects and dict format
            reference = getattr(component, "reference", None) or component.get("reference")

            if reference not in component_names:
                invalid_references.append(reference)

        if invalid_references:
            available_components = list(component_names) if component_names else ["none"]
            project_name = project_data.get("name", "unknown")
            error_msg = f"Invalid component references in {context} for project '{project_name}': {invalid_references}. Available components: {available_components}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg, "invalid_references": invalid_references}

        return {"success": True, "error": None, "invalid_references": None}

    @deprecated("We most likely need to use get_contents() instead")
    async def get_project_data(self, project_name: str) -> dict[str, Any]:
        """
        Retrieve and parse project data from Git repository.

        This is a foundational method that handles the common pattern of:
        1. Creating git connector
        2. Reading project YAML file
        3. Parsing the content

        Args:
            project_name: Name of the project

        Returns:
            Parsed project data as dictionary

        Raises:
            HTTPException: If project not found or parsing fails
        """
        try:
            # TODO: replace this logic with the correct method calls.. this should not be done this way!!
            # Create git connector to read from projects repository
            git_connector = GitConnector(
                repo_url=settings.GIT_PROJECTS_SERVER_URL,
                username=settings.GIT_PROJECTS_SERVER_USERNAME,
                password=settings.GIT_PROJECTS_SERVER_PASSWORD,
                branch=settings.GIT_PROJECTS_SERVER_BRANCH,
                repo_path=settings.GIT_PROJECTS_SERVER_REPO_PATH,
                project_name=project_name,  # Add project context for data retrieval operations
            )

            # Read the project file
            project_file_path = f"projects/{project_name}.yaml"
            project_content = await git_connector.read_file_content(project_file_path)
            if not project_content:
                raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")

            # Parse the project YAML
            yaml = YAML()
            project_data = yaml.load(project_content)
            return project_data

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Error reading project data for {project_name}: {e}")
            raise HTTPException(status_code=500, detail=f"Error reading project data: {e!s}")

    async def update_project_field_by_path(
        self, project_name: str, json_path: str, new_value: Any, commit_message: str
    ) -> bool:
        """
        Update a specific field in a project file using JSON path and commit the change.

        Args:
            project_name: Name of the project
            json_path: JSON path to the field (e.g., "deployments[?(@.name=='api')].components[?(@.reference=='backend')].image")
            new_value: New value to set
            commit_message: Commit message for the change

        Returns:
            True if update was successful, False otherwise
        """
        try:
            project_data = await self.get_contents()
            jsonpath_expr = jsonpath_parse(json_path)
            matches = jsonpath_expr.find(project_data)

            if not matches:
                logger.error(f"JSON path '{json_path}' not found in project {project_name}")
                return False

            # Update the first match (there should typically be only one)
            matches[0].full_path.update(project_data, new_value)

            logger.info(f"Successfully updated {json_path} in project {project_name}")
            return True

        except Exception as e:
            logger.exception(f"Error updating project field for {project_name}: {e}")
            return False

    async def find_value_by_jsonpath(self, json_path: str, default: Any = None) -> Any:
        """
        Retrieve a specific value from project configuration using JSONPath.

        This method provides a reusable way to extract any value from the project YAML
        using JSONPath expressions like 'config.api-key' or 'deployments[0].cluster'.

        Args:
            json_path: JSONPath expression to extract the value
            default: Default value to return if path not found

        Returns:
            The extracted value or default if not found

        Raises:
            HTTPException: If project not found or parsing fails
        """
        try:
            project_data = await self.get_contents()
            return find_value_by_jsonpath(project_data, json_path, default)
        except Exception as e:
            logger.exception(f"Error extracting value from project at path '{json_path}': {e}")
            raise HTTPException(status_code=500, detail=f"Error extracting project value: {e!s}")

    async def clone_deployment(
        self, project_name: str, target_deployment_name: str, source_deployment_name: str, force_clone: bool = False
    ) -> dict[str, Any]:
        """
        Clone resources from source deployment to target deployment.

        This method orchestrates cloning of:
        1. Database resources (schema, user, data)
        2. MinIO resources (bucket, user, objects)

        By default, cloning only happens on initial setup when target resources don't exist yet.
        Use force_clone=True to clone even if target resources already exist.

        Args:
            project_name: Name of the project
            target_deployment_name: Name of the target deployment
            source_deployment_name: Name of the source deployment to clone from
            force_clone: If True, clone even if target resources already exist (default: False)

        Returns:
            Dictionary containing clone results and status

        Raises:
            HTTPException: If critical operations fail
        """
        clone_results = {
            "project": project_name,
            "source_deployment": source_deployment_name,
            "target_deployment": target_deployment_name,
            "operations": [],
            "success": True,
            "errors": [],
        }

        logger.info(
            f"Starting clone operation: {source_deployment_name} -> {target_deployment_name} for project {project_name}"
        )

        try:
            # Load project data
            project_data = await self.config_handler.get_project_file_content(project_name)
            if not project_data:
                raise HTTPException(status_code=404, detail=f"Project {project_name} not found")

            # Find target deployment
            target_deployment = None
            for deployment in project_data.get("deployments", []):
                if deployment.get("name") == target_deployment_name:
                    target_deployment = deployment
                    break

            if not target_deployment:
                raise HTTPException(status_code=404, detail=f"Target deployment {target_deployment_name} not found")

            # Verify source deployment exists
            source_deployment_exists = False
            for deployment in project_data.get("deployments", []):
                if deployment.get("name") == source_deployment_name:
                    source_deployment_exists = True
                    break

            if not source_deployment_exists:
                raise HTTPException(status_code=404, detail=f"Source deployment {source_deployment_name} not found")

            # Clone database resources if target deployment uses PostgreSQL
            if await self.database_manager._deployment_uses_postgresql(project_data, target_deployment_name):
                try:
                    logger.info(f"Cloning database resources from {source_deployment_name} to {target_deployment_name}")
                    await self.database_manager.clone_database_from_deployment(
                        project_data, target_deployment, source_deployment_name, force_clone
                    )
                    clone_results["operations"].append(
                        {
                            "type": "database_clone",
                            "status": "success",
                            "message": f"Successfully cloned database from {source_deployment_name}",
                        }
                    )
                except Exception as e:
                    logger.exception(f"Failed to clone database resources: {e}")
                    clone_results["errors"].append(f"Database clone failed: {e}")
                    clone_results["operations"].append({"type": "database_clone", "status": "failed", "error": str(e)})

            # Clone MinIO resources if target deployment uses MinIO
            if await self._minio_manager._deployment_uses_minio(project_data, target_deployment_name):
                try:
                    logger.info(f"Cloning MinIO resources from {source_deployment_name} to {target_deployment_name}")
                    await self._minio_manager.clone_minio_from_deployment(
                        project_data, target_deployment, source_deployment_name
                    )
                    clone_results["operations"].append(
                        {
                            "type": "minio_clone",
                            "status": "success",
                            "message": f"Successfully cloned MinIO from {source_deployment_name}",
                        }
                    )
                except Exception as e:
                    logger.exception(f"Failed to clone MinIO resources: {e}")
                    clone_results["errors"].append(f"MinIO clone failed: {e}")
                    clone_results["operations"].append({"type": "minio_clone", "status": "failed", "error": str(e)})

            # Update success status based on errors
            clone_results["success"] = len(clone_results["errors"]) == 0

            if clone_results["success"]:
                logger.info(
                    f"Clone operation completed successfully: {source_deployment_name} -> {target_deployment_name}"
                )
            else:
                logger.warning(f"Clone operation completed with errors: {clone_results['errors']}")

            return clone_results

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Error during clone operation: {e}")
            raise HTTPException(status_code=500, detail=f"Clone operation failed: {e!s}") from e

    async def validate_project_api_key(self, project_name: str, provided_api_key: str) -> bool:
        """
        Validate that the provided API key matches the project's API key.

        This method uses the reusable encrypted value retrieval system to get and decrypt
        the project's API key, then compares it with the provided key.

        Args:
            project_name: Name of the project
            provided_api_key: API key provided in the request header

        Returns:
            True if the API key is valid

        Raises:
            HTTPException: If project not found or API key is invalid
        """
        try:
            # Get the raw API key value
            raw_api_key = await self.find_value_by_jsonpath(project_name, "config.api-key")
            if raw_api_key is None:
                raise HTTPException(status_code=404, detail=f"No API key found for project '{project_name}'")

            # Use the smart decryption logic from age.py
            decrypted_api_key = await decrypt_password_smart_auto(str(raw_api_key))

            # Compare API keys
            if decrypted_api_key != provided_api_key:
                raise HTTPException(status_code=401, detail="Invalid project API key")

            logger.debug(f"Project API key validated successfully for project: {project_name}")
            return True

        except HTTPException:
            raise
        except Exception as e:
            logger.exception(f"Error validating project API key for {project_name}: {e}")
            raise HTTPException(status_code=500, detail=f"Error validating project API key: {e!s}") from e


def create_project_manager() -> ProjectManager:
    """
    Create and return a ProjectManager instance.

    Returns:
        ProjectManager instance
    """
    logger.debug("Creating ProjectManager")
    return ProjectManager()
