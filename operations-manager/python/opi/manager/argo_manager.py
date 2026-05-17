"""ArgoCD manager for handling ArgoCD resources and repository secrets."""

import logging
import os
from typing import Any, cast

from opi.core.cluster_config import get_argo_namespace, get_prefixed_namespace
from opi.core.config import settings
from opi.utils.age import decrypt_password_smart
from opi.utils.naming import (
    generate_argocd_application_name,
    generate_argocd_appproject_prefix,
    generate_argocd_repository_secret_name,
    generate_infrastructure_application_name,
    generate_infrastructure_argocd_application_filename,
    generate_infrastructure_argocd_appproject_filename,
    generate_infrastructure_argocd_folder_path,
    get_output_filename_from_template,
    make_argocd_repository_url_unique,
)
from opi.utils.sops import encrypt_to_sops_files

logger = logging.getLogger(__name__)


class ArgoManager:
    """Manager for ArgoCD-related operations and resources."""

    def __init__(self, project_manager: "ProjectManager") -> None:
        """
        Initialize the ArgoManager with reference to ProjectManager.

        Args:
            project_manager: The main ProjectManager instance for accessing shared resources
        """
        self.project_manager = project_manager

    async def create_argocd_resources(self, deployment_name: str | None = None) -> None:
        """
        Create all ArgoCD resources for this project.

        In a Distributed Operations Manager architecture, this creates ArgoCD resources
        only for deployments in the CLUSTER_MANAGER cluster.

        Args:
            deployment_name: Optional deployment name to create resources only for specific deployment
        """
        project_name = await self.project_manager.get_name()
        logger.info(f"Creating ArgoCD resources for {project_name} on cluster {settings.CLUSTER_MANAGER}")

        project_data = await self.project_manager.get_contents()

        await self.create_repository_secrets(project_data, deployment_name)
        await self.create_app_projects(project_data, deployment_name)
        await self.create_applications(project_data, deployment_name)
        await self.create_kustomization_files(project_data, deployment_name)
        await (await self.project_manager.get_git_connector_for_argocd()).commit_and_push(
            f"Added ArgoCD resources for project {project_name} on cluster {settings.CLUSTER_MANAGER}"
        )

    async def create_repository_secrets(self, project_data: dict[str, Any], deployment_name: str | None = None) -> None:
        """
        Create ArgoCD repository secrets with unique URLs for each project.

        This method creates ArgoCD repository manifest files for each repository defined
        in the project. Each repository URL is made unique by embedding the project name
        as a username, preventing ArgoCD credential conflicts when multiple projects
        use the same repository URL.

        Repository files are created with SOPS encryption using .to-sops.yaml naming convention.

        In a Distributed Operations Manager architecture, this method only creates secrets
        for deployments in the CLUSTER_MANAGER cluster.

        Args:
            project_data: The project configuration data containing repositories and deployments
            deployment_name: Optional deployment name to filter by specific deployment
        """
        project_name = await self.project_manager.get_name()

        # Get deployments for THIS cluster only
        deployments = await self.project_manager.get_deployments(cluster_filter=True, deployment_name=deployment_name)

        if not deployments:
            logger.warning(f"No deployments for cluster {settings.CLUSTER_MANAGER}")
            return

        git_connector_for_argocd = await self.project_manager.get_git_connector_for_argocd()
        working_dir = await git_connector_for_argocd.get_working_dir()

        logger.info(
            f"Creating ArgoCD repository secrets for project: {project_name} on cluster {settings.CLUSTER_MANAGER}"
        )

        # Get all repositories from the project data
        repositories = await self.project_manager.get_repositories()
        if not repositories:
            logger.error("No repositories defined in project data")
            return

        # Use CLUSTER_MANAGER directly - this instance only manages one cluster
        cluster_name = settings.CLUSTER_MANAGER
        project_dir = os.path.join(str(working_dir), str(cluster_name), str(project_name))
        logger.info(f"Creating cluster/project directory: {project_dir}")
        os.makedirs(project_dir, exist_ok=True)

        # Prepare manifest configurations for batch creation
        manifest_configs = []
        for repository in repositories:
            repo_name = repository["name"]
            repo_url = repository["url"]

            logger.info(f"  Processing repository: {repo_name} ({repo_url})")

            # Generate unique, sanitized name WITHOUT credential hash
            # ArgoCD properly reloads secrets when they change, so versioning is not needed
            unique_repo_name = generate_argocd_repository_secret_name(project_name, repo_name)
            logger.info(f"  Generated secret name: {unique_repo_name}")

            # Prepare variables for the manifest template (includes URL uniquification)
            variables = await self.prepare_repository_variables(
                name=unique_repo_name,
                namespace=get_argo_namespace(cluster_name),
                repository=repository,
                repo_type="git",
                project_name=project_name,
            )

            # Determine template path based on authentication method
            is_https = repo_url.startswith("https://")
            template_filename = "argo-repository-https.yaml.jinja" if is_https else "argo-repository.yaml.jinja"
            template_path = os.path.join(settings.MANIFESTS_PATH, template_filename)
            output_filename = get_output_filename_from_template(template_filename, unique_repo_name)

            logger.info(f"  Output filename: {output_filename}")

            # No cleanup needed - ArgoCD properly reloads secrets when they change
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
            manifest_path = self.project_manager._manifest_generator.create_manifest_file(
                template_path=config["template_path"],
                values=config["values"],
                output_dir=project_dir,
                output_filename=config["output_filename"],
                use_sops=config["use_sops"],
            )
            created_files.append(manifest_path)
            logger.info(f"Successfully created repository manifest: {os.path.basename(manifest_path)}")

        encrypt_to_sops_files(project_dir, cast("str", settings.SOPS_AGE_PUBLIC_KEY))

    async def prepare_repository_variables(
        self, name: str, namespace: str, repository: dict[str, Any], repo_type: str, project_name: str
    ) -> dict[str, Any]:
        """
        Prepare variables for ArgoCD repository manifest templates.

        This method generates the template variables needed to create an ArgoCD repository
        secret. It makes the repository URL unique by embedding the project name as a username,
        preventing ArgoCD credential conflicts when multiple projects use the same repository.

        Args:
            name: Repository secret name
            namespace: ArgoCD namespace
            repository: Repository configuration dictionary containing url, username, password
            repo_type: Repository type (e.g., "git", "helm")
            project_name: Project name to make URL unique

        Returns:
            Dictionary containing variables for template substitution with unique URL
        """
        repository_url = repository.get("url", "")
        username = repository.get("username")
        password = repository.get("password")

        # Make URL unique for ArgoCD by embedding project name as username
        # This prevents "Found multiple credentials for repoURL" warnings
        unique_url = make_argocd_repository_url_unique(repository_url, project_name)

        logger.debug(f"Original URL: {repository_url}")
        logger.debug(f"Unique URL for ArgoCD: {unique_url}")

        # Determine authentication method
        is_https = unique_url.startswith("https://")

        # Decrypt password if encrypted
        decrypted_password = None
        if password:
            decrypted_password = await decrypt_password_smart(password, settings.SOPS_AGE_PRIVATE_KEY)

        # SSH deploy key for git repositories. Read from the configured key
        # path at render time instead of hard-coding it in the template, so no
        # private key material lives in version control. The rendered secret is
        # SOPS-encrypted before being written (use_sops=True).
        ssh_private_key = ""
        if not is_https:
            ssh_key_path = settings.GIT_SERVER_KEY_PATH
            try:
                with open(ssh_key_path) as f:
                    ssh_private_key = f.read()
            except OSError as e:
                raise RuntimeError(f"Cannot read git SSH deploy key from {ssh_key_path} for repository '{name}'") from e

        # Prepare variables for template
        return {
            "name": name,
            "namespace": namespace,
            "type": repo_type,
            "repository_url": unique_url,  # UNIQUE URL for ArgoCD
            "is_https": is_https,
            "username": username or "",
            "password": decrypted_password or "",
            "ssh_private_key": ssh_private_key,
        }

    async def generate_repository_manifest(
        self, name: str, namespace: str, repository: dict[str, Any], repo_type: str, project_name: str
    ) -> str:
        """
        Generate an ArgoCD repository manifest using the template file.

        Supports both SSH and HTTPS authentication methods.

        Args:
            name: Repository name
            namespace: ArgoCD namespace
            repository: Repository configuration dictionary
            repo_type: Repository type (e.g., "git")
            project_name: Project name to make URL unique

        Returns:
            String containing the YAML manifest
        """
        repository_url = repository.get("url", "")

        # Prepare variables with unique URL
        variables = await self.prepare_repository_variables(name, namespace, repository, repo_type, project_name)

        # Choose template based on authentication method
        is_https = repository_url.startswith("https://")

        if is_https:
            # Use HTTPS template for all HTTPS repositories
            manifest_path = os.path.join(settings.MANIFESTS_PATH, "argo-repository-https.yaml.jinja")
            logger.info(
                f"Using HTTPS template for repository {name} (credentials: "
                f"{'YES' if variables.get('username') and variables.get('password') else 'NO'})"
            )
        else:
            # Use SSH template for SSH and git:// repositories
            manifest_path = os.path.join(settings.MANIFESTS_PATH, "argo-repository.yaml.jinja")
            logger.info(f"Using SSH template for repository {name}")

        # Read and process the manifest template
        try:
            with open(manifest_path) as f:
                manifest_template = f.read()

            # Process the template with the variables
            return self.project_manager._manifest_generator.template_manifest(manifest_template, variables)
        except Exception:
            logger.exception("Error generating ArgoCD repository manifest")
            raise

    async def create_app_projects(self, project_data: dict[str, Any], deployment_name: str | None = None) -> None:
        """
        Create ArgoCD AppProject resources for the project.

        In a Distributed Operations Manager architecture, this method only creates AppProjects
        for deployments in the CLUSTER_MANAGER cluster.

        Args:
            project_data: The project configuration data
            deployment_name: Optional deployment name to filter by specific deployment
        """
        project_name = await self.project_manager.get_name()
        logger.debug(f"Creating ArgoCD AppProject for project: {project_name} on cluster {settings.CLUSTER_MANAGER}")

        # Get deployments for THIS cluster only
        deployments = await self.project_manager.get_deployments(cluster_filter=True, deployment_name=deployment_name)

        if not deployments:
            logger.warning(f"No deployments for cluster {settings.CLUSTER_MANAGER}")
            return

        # Collect unique namespaces from deployments
        base_namespaces: set[str] = set()
        for deployment in deployments:
            base_namespace = deployment.get("namespace")
            base_namespaces.add(base_namespace)

        git_connector_for_argocd = await self.project_manager.get_git_connector_for_argocd()
        working_dir = await git_connector_for_argocd.get_working_dir()

        # Use CLUSTER_MANAGER directly - this instance only manages one cluster
        cluster_name = settings.CLUSTER_MANAGER

        # Create one AppProject per namespace in THIS cluster
        for base_destination_namespace in base_namespaces:
            # Generate consistent project-namespace name for AppProject
            appproject_name = generate_argocd_appproject_prefix(project_name, base_destination_namespace)

            # Create ArgoCD AppProject manifest content
            appproject_content = self.generate_appproject_manifest(
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

            appproject_file_path = os.path.join(project_dir, output_filename)
            with open(appproject_file_path, "w") as f:
                f.write(appproject_content)

            logger.info(
                f"Successfully created ArgoCD AppProject file for cluster {cluster_name}, "
                f"namespace {base_destination_namespace}: {appproject_file_path}"
            )

    def generate_appproject_manifest(
        self, name: str, namespace: str, destination_namespace: str, project_label: str
    ) -> str:
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
            "destination": {"namespace": destination_namespace},
            "labels": {"project": project_label},
        }

        # Read and process the manifest template
        try:
            with open(manifest_path) as f:
                manifest_template = f.read()

            # Process the template with the variables
            return self.project_manager._manifest_generator.template_manifest(manifest_template, variables)
        except Exception:
            logger.exception("Error generating ArgoCD AppProject manifest")
            raise

    def generate_application_manifest(
        self,
        name: str,
        namespace: str,
        argo_project: str,
        repo_url: str,
        target_revision: str,
        repo_path: str,
        destination_namespace: str,
        project_label: str,
    ) -> str:
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

        # Read and process the manifest template
        try:
            with open(manifest_path) as f:
                manifest_template = f.read()

            # Process the template with the variables
            return self.project_manager._manifest_generator.template_manifest(manifest_template, variables)
        except Exception:
            logger.exception("Error generating ArgoCD application manifest")
            raise

    async def create_applications(self, project_data: dict[str, Any], deployment_name: str | None = None) -> bool:
        """
        Create ArgoCD Application resources for the project.

        In a Distributed Operations Manager architecture, this method only creates applications
        for deployments in the CLUSTER_MANAGER cluster.

        Args:
            project_data: The project configuration data
            deployment_name: Optional deployment name to create application only for specific deployment

        Returns:
            True if all applications were created successfully, False otherwise
        """
        project_name = await self.project_manager.get_name()
        logger.debug(f"Creating ArgoCD application for project: {project_name}")

        git_connector_for_argocd = await self.project_manager.get_git_connector_for_argocd()
        working_dir = await git_connector_for_argocd.get_working_dir()

        try:
            # Create an ArgoCD application for each deployment (filtered by cluster and optional name)
            deployments = await self.project_manager.get_deployments(
                cluster_filter=True, deployment_name=deployment_name
            )
            if deployment_name:
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
                repositories = project_data.get("repositories", [])
                repo_info = next((r for r in repositories if r.get("name") == repo_name), None)

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

                # Make repository URL unique for ArgoCD (must match the repository secret URL)
                repo_url = make_argocd_repository_url_unique(repo_info.get("url"), project_name)
                logger.debug(f"Using unique repository URL for ArgoCD application: {repo_url}")

                # Create ArgoCD application manifest content
                argocd_app_content = self.generate_application_manifest(
                    name=app_name,
                    namespace=get_argo_namespace(cluster_name),
                    argo_project=generate_argocd_appproject_prefix(project_name, base_namespace),
                    repo_url=repo_url,
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

    async def create_kustomization_files(
        self, project_data: dict[str, Any], deployment_name: str | None = None
    ) -> None:
        """
        Create kustomization.yaml files for ArgoCD project folders.

        In a Distributed Operations Manager architecture, this method only creates kustomization
        files for the CLUSTER_MANAGER cluster.

        This method lists all YAML files in the project folder and creates a kustomization.yaml
        that includes all ArgoCD manifests (applications, repositories, appprojects).

        Args:
            project_data: The project configuration data
            deployment_name: Optional deployment name (currently unused, for future filtering)
        """
        git_connector_for_argocd = await self.project_manager.get_git_connector_for_argocd()
        working_dir = await git_connector_for_argocd.get_working_dir()

        project_name = await self.project_manager.get_name()

        # Use CLUSTER_MANAGER directly - this instance only manages one cluster
        cluster_name = settings.CLUSTER_MANAGER
        project_dir = os.path.join(str(working_dir), str(cluster_name), str(project_name))

        self.project_manager._manifest_generator.create_kustomization_files(
            output_dir=project_dir,
            namespace=get_argo_namespace(cluster_name),  # Use ArgoCD namespace for the cluster
        )

        logger.info(
            f"Successfully created ArgoCD kustomization.yaml for project {project_name} for cluster {cluster_name}"
        )

    async def create_infrastructure_application(
        self, project_data: dict[str, Any], database_config: dict[str, Any], cluster_name: str
    ) -> bool:
        """
        Create ArgoCD infrastructure application and AppProject for namespace-specific database.

        This creates a separate ArgoCD application with sync-wave 0 that manages infrastructure
        resources (CNPG database cluster, superuser credentials) in the infrastructure namespace.

        Creates in ArgoCD applications repo at {cluster}/{project_name}-infrastructure/:
        - ArgoCD Application manifest
        - ArgoCD AppProject manifest
        - Repository secret (SOPS encrypted)
        - Kustomization.yaml

        Args:
            project_data: The project configuration data
            database_config: Database configuration from parsed services (image, storage, instances)
            cluster_name: Target cluster name

        Returns:
            True if infrastructure application was created successfully, False otherwise
        """
        from opi.core.cluster_config import get_infrastructure_namespace

        project_name = await self.project_manager.get_name()
        logger.info(f"Creating ArgoCD infrastructure application for project: {project_name}")

        git_connector_for_argocd = await self.project_manager.get_git_connector_for_argocd()
        working_dir = await git_connector_for_argocd.get_working_dir()

        try:
            # Get repository information (use first repository, typically main-repo)
            repositories = project_data.get("repositories", [])
            if not repositories:
                raise ValueError("No repositories defined in project data")
            repo_info = repositories[0]  # Use first repository

            # Infrastructure namespace (cluster-aware)
            infrastructure_namespace = get_infrastructure_namespace(cluster_name, project_name)

            # ArgoCD application name for infrastructure
            app_name = f"{project_name}-infrastructure"

            # Repository path for infrastructure resources
            repo_path = repo_info.get("path", "")
            if repo_path:
                infrastructure_path = f"{repo_path}/{cluster_name}/{project_name}/infrastructure"
            else:
                infrastructure_path = f"{cluster_name}/{project_name}/infrastructure"

            # Make repository URL unique for ArgoCD
            repo_url = make_argocd_repository_url_unique(repo_info.get("url"), project_name)

            # Create directory for infrastructure ArgoCD resources
            # Path: {cluster}/{project_name}-infrastructure/
            # This follows the App-of-Apps pattern where each subfolder becomes an app
            infra_folder_relative = generate_infrastructure_argocd_folder_path(cluster_name, project_name)
            infra_argo_dir = os.path.join(str(working_dir), infra_folder_relative)
            os.makedirs(infra_argo_dir, exist_ok=True)

            logger.info(f"Creating ArgoCD infrastructure resources in: {infra_argo_dir}")

            # Create infrastructure AppProject
            appproject_name = generate_infrastructure_application_name(project_name)
            appproject_content = self.generate_appproject_manifest(
                name=appproject_name,
                namespace=get_argo_namespace(cluster_name),
                destination_namespace=infrastructure_namespace,
                project_label=project_name,
            )

            # Write AppProject
            appproject_filename = generate_infrastructure_argocd_appproject_filename(project_name)
            appproject_file_path = os.path.join(infra_argo_dir, appproject_filename)
            with open(appproject_file_path, "w") as f:
                f.write(appproject_content)

            logger.info(f"Created infrastructure AppProject: {appproject_file_path}")

            # Create repository secret for infrastructure (same as normal deployments)
            repo_name = repo_info.get("name", "main-repo")
            unique_repo_name = generate_argocd_repository_secret_name(project_name, repo_name)

            # Prepare variables for the manifest template
            variables = await self.prepare_repository_variables(
                name=unique_repo_name,
                namespace=get_argo_namespace(cluster_name),
                repository=repo_info,
                repo_type="git",
                project_name=project_name,
            )

            # Determine template path based on authentication method
            is_https = repo_info.get("url", "").startswith("https://")
            template_filename = "argo-repository-https.yaml.jinja" if is_https else "argo-repository.yaml.jinja"
            template_path = os.path.join(settings.MANIFESTS_PATH, template_filename)
            output_filename = get_output_filename_from_template(template_filename, unique_repo_name)

            # Create repository secret with SOPS encryption
            repo_secret_path = self.project_manager._manifest_generator.create_manifest_file(
                template_path=template_path,
                values=variables,
                output_dir=infra_argo_dir,
                output_filename=output_filename,
                use_sops=True,
            )
            logger.info(f"Created repository secret: {os.path.basename(repo_secret_path)}")

            # SOPS encrypt the repository secret
            encrypt_to_sops_files(infra_argo_dir, cast("str", settings.SOPS_AGE_PUBLIC_KEY))

            # Create infrastructure Application (with sync-wave: 0)
            argocd_app_content = self.generate_application_manifest(
                name=app_name,
                namespace=get_argo_namespace(cluster_name),
                argo_project=appproject_name,
                repo_url=repo_url,
                target_revision=repo_info.get("branch", "main"),
                repo_path=infrastructure_path,
                destination_namespace=infrastructure_namespace,
                project_label=project_name,
            )

            # Modify the generated manifest to add sync-wave: 0 (infrastructure first)
            # The template already has sync-wave: 1, we need to change it to 0
            argocd_app_content = argocd_app_content.replace(
                'argocd.argoproj.io/sync-wave: "1"', 'argocd.argoproj.io/sync-wave: "0"'
            )

            # Write Application
            app_filename = generate_infrastructure_argocd_application_filename(project_name)
            app_file_path = os.path.join(infra_argo_dir, app_filename)

            with open(app_file_path, "w") as f:
                f.write(argocd_app_content)

            logger.info(f"Created infrastructure Application: {app_file_path}")

            # Create kustomization.yaml for the infrastructure ArgoCD folder
            kustomization_success = self.project_manager._manifest_generator.create_kustomization_files(
                output_dir=infra_argo_dir,
                namespace=None,  # No namespace override for ArgoCD resources
            )
            if not kustomization_success:
                raise RuntimeError("Failed to create kustomization files for infrastructure ArgoCD resources")

            logger.info("Created kustomization.yaml for infrastructure ArgoCD resources")

            # Commit and push all infrastructure ArgoCD resources
            await git_connector_for_argocd.commit_and_push(
                f"Add ArgoCD infrastructure application for {project_name} in {cluster_name} cluster"
            )

            logger.info(f"Successfully committed ArgoCD infrastructure resources for project '{project_name}'")

            return True

        except Exception as e:
            logger.exception(f"Error creating infrastructure ArgoCD application: {e}")
            return False

    async def wait_for_application_created(self, app_name: str, timeout: int = 120, poll_interval: int = 5) -> bool:
        """
        Wait for an ArgoCD application to be created and appear in the API.

        This method polls the ArgoCD API until the application exists or timeout is reached.
        Used after creating ArgoCD application manifests to wait for ArgoCD to detect them.

        Args:
            app_name: Name of the application to wait for
            timeout: Maximum time to wait in seconds (default: 120 = 2 minutes)
            poll_interval: Seconds between checks (default: 5)

        Returns:
            True if application was created, False otherwise

        Raises:
            TimeoutError: If application is not created within timeout
        """
        from opi.connectors.argo import ArgoConnector

        logger.info(
            f"Waiting for ArgoCD application '{app_name}' to be created (timeout: {timeout}s, poll: {poll_interval}s)"
        )

        # Create ArgoConnector instance
        argo_connector = ArgoConnector(
            server_host=settings.ARGOCD_HOST,
            server_port=settings.ARGOCD_PORT,
            username=settings.ARGOCD_USERNAME,
            password=settings.ARGOCD_PASSWORD,
            use_tls=settings.ARGOCD_USE_TLS,
            verify_ssl=settings.ARGOCD_VERIFY_SSL,
        )

        # Login to ArgoCD
        if not await argo_connector.login():
            raise RuntimeError("Failed to login to ArgoCD")

        import asyncio

        elapsed_time = 0
        while elapsed_time < timeout:
            try:
                # Check if application exists
                if await argo_connector.application_exists(app_name):
                    logger.info(f"ArgoCD application '{app_name}' has been created!")
                    return True

                # Not created yet, wait and retry
                logger.debug(
                    f"Application '{app_name}' not found yet, waiting {poll_interval}s... (elapsed: {elapsed_time}s)"
                )
                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval

            except Exception as e:
                logger.warning(f"Error checking application creation status: {e}, retrying...")
                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval

        # Timeout reached
        error_msg = f"Timeout waiting for ArgoCD application '{app_name}' to be created after {timeout}s"
        logger.error(error_msg)
        raise TimeoutError(error_msg)

    async def wait_for_infrastructure_ready(
        self,
        project_name: str,
        cluster_name: str,
        timeout: int = 300,
        poll_interval: int = 10,
        refreshed_after: str | None = None,
    ) -> bool:
        """
        Wait for infrastructure ArgoCD application to be synced and healthy.

        Polls the ArgoCD API to check if the infrastructure application (database cluster)
        is successfully deployed and healthy before proceeding with application deployment.

        Args:
            project_name: Name of the project
            cluster_name: Target cluster name
            timeout: Maximum time to wait in seconds (default: 300 = 5 minutes)
            poll_interval: Seconds between status checks (default: 10)
            refreshed_after: ISO-8601 ``reconciledAt`` timestamp returned by
                ``refresh_application``.  When set, terminal states are ignored
                until ``reconciledAt`` moves past this value.

        Returns:
            True if infrastructure is healthy, False if timeout or error

        Raises:
            TimeoutError: If infrastructure doesn't become ready within timeout
            RuntimeError: If infrastructure application fails to sync or becomes unhealthy
        """
        from opi.connectors.argo import ArgoConnector

        app_name = f"{project_name}-infrastructure"
        logger.info(
            f"Waiting for infrastructure application '{app_name}' to be ready "
            f"(timeout: {timeout}s, poll: {poll_interval}s, refreshed_after={refreshed_after})"
        )

        # Create ArgoConnector instance
        argo_connector = ArgoConnector(
            server_host=settings.ARGOCD_HOST,
            server_port=settings.ARGOCD_PORT,
            username=settings.ARGOCD_USERNAME,
            password=settings.ARGOCD_PASSWORD,
            use_tls=settings.ARGOCD_USE_TLS,
            verify_ssl=settings.ARGOCD_VERIFY_SSL,
        )

        # Login to ArgoCD
        if not await argo_connector.login():
            raise RuntimeError(f"Failed to login to ArgoCD for cluster {cluster_name}")

        import asyncio

        elapsed_time = 0
        while elapsed_time < timeout:
            try:
                # Get application status
                status_data = await argo_connector.get_application_status(app_name)

                if not status_data:
                    logger.warning(f"Could not get status for application {app_name}, retrying...")
                    await asyncio.sleep(poll_interval)
                    elapsed_time += poll_interval
                    continue

                # Check sync status
                sync_status = status_data.get("status", {}).get("sync", {}).get("status")
                health_status = status_data.get("status", {}).get("health", {}).get("status")

                logger.info(f"Infrastructure status: sync={sync_status}, health={health_status}")

                # Check if synced and healthy
                if sync_status == "Synced" and health_status == "Healthy":
                    logger.info(f"Infrastructure application '{app_name}' is ready!")
                    return True

                # Determine whether the status reflects our refresh.
                status_is_fresh = True
                if refreshed_after:
                    reconciled_at = status_data.get("status", {}).get("reconciledAt", "")
                    if reconciled_at <= refreshed_after:
                        status_is_fresh = False
                        logger.debug(
                            f"Infrastructure '{app_name}': status is stale "
                            f"(reconciledAt={reconciled_at} <= refreshed_after={refreshed_after})"
                        )

                if status_is_fresh:
                    # Check for sync operation failures (unrecoverable errors)
                    operation_state = status_data.get("status", {}).get("operationState", {})
                    operation_phase = operation_state.get("phase")
                    operation_message = operation_state.get("message", "")

                    if operation_phase in ("Failed", "Error"):
                        error_msg = f"Infrastructure application '{app_name}' sync failed: {operation_phase}"
                        logger.error(error_msg)
                        if operation_message:
                            logger.error(f"  Sync error: {operation_message}")
                        # Log sync result details if available
                        sync_result = operation_state.get("syncResult", {})
                        if sync_result:
                            resources = sync_result.get("resources", [])
                            for resource in resources:
                                if resource.get("status") == "SyncFailed":
                                    kind = resource.get("kind")
                                    name = resource.get("name")
                                    msg = resource.get("message")
                                    logger.error(f"  Resource failed: {kind}/{name} - {msg}")
                        raise RuntimeError(error_msg)

                    # Check for failure states - only Degraded is a true failure
                    # "Missing" means resources are still being created, which is expected during initial deployment
                    if health_status == "Degraded":
                        error_msg = f"Infrastructure application '{app_name}' is in failed state: {health_status}"
                        logger.error(error_msg)
                        # Get more details from status
                        conditions = status_data.get("status", {}).get("conditions", [])
                        for condition in conditions:
                            logger.error(f"  Condition: {condition.get('type')} - {condition.get('message')}")
                        raise RuntimeError(error_msg)

                # Not ready yet, wait and retry
                # Log different messages based on health status for clarity
                if health_status == "Missing":
                    logger.info(
                        f"Infrastructure resources are being created, waiting {poll_interval}s... (elapsed: {elapsed_time}s)"
                    )
                elif health_status == "Progressing":
                    logger.info(
                        f"Infrastructure resources are progressing, waiting {poll_interval}s... (elapsed: {elapsed_time}s)"
                    )
                else:
                    logger.debug(
                        f"Infrastructure not ready yet (health={health_status}, fresh={status_is_fresh}), "
                        f"waiting {poll_interval}s... (elapsed: {elapsed_time}s)"
                    )
                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval

            except RuntimeError:
                # Re-raise RuntimeError (failure states)
                raise
            except Exception as e:
                logger.warning(f"Error checking infrastructure status: {e}, retrying...")
                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval

        # Timeout reached
        error_msg = f"Timeout waiting for infrastructure application '{app_name}' to be ready after {timeout}s"
        logger.error(error_msg)
        # Construct ArgoCD dashboard URL from settings
        protocol = "https" if settings.ARGOCD_USE_TLS else "http"
        port_suffix = "" if settings.ARGOCD_PORT in (80, 443) else f":{settings.ARGOCD_PORT}"
        argocd_url = f"{protocol}://{settings.ARGOCD_HOST}{port_suffix}"
        logger.error(f"ArgoCD dashboard: {argocd_url}/applications/{app_name}")
        raise TimeoutError(error_msg)

    async def wait_for_application_synced(
        self,
        app_name: str,
        timeout: int = 300,
        poll_interval: int = 5,
        refreshed_after: str | None = None,
    ) -> bool:
        """
        Wait for an ArgoCD application to be synced and healthy.

        Polls the ArgoCD API until the application reaches Synced+Healthy state,
        or until a terminal failure is detected.

        Args:
            app_name: Name of the application to wait for
            timeout: Maximum time to wait in seconds (default: 300 = 5 minutes)
            poll_interval: Seconds between status checks (default: 5)
            refreshed_after: ISO-8601 ``reconciledAt`` timestamp returned by
                ``refresh_application``.  When set, the poll loop will not
                treat ``Degraded`` or ``Failed``/``Error`` as terminal until
                ArgoCD has reconciled *after* this timestamp.  This prevents
                false failures when the status still reflects a previous
                reconciliation.

        Returns:
            True if application is synced and healthy

        Raises:
            TimeoutError: If application doesn't become ready within timeout
            RuntimeError: If application enters a terminal failure state
        """
        from opi.connectors.argo import create_argo_connector

        logger.info(
            f"Waiting for ArgoCD application '{app_name}' to be synced and healthy "
            f"(timeout: {timeout}s, poll: {poll_interval}s, refreshed_after={refreshed_after})"
        )

        argo_connector = create_argo_connector()
        if not await argo_connector.login():
            raise RuntimeError("Failed to login to ArgoCD")

        import asyncio

        elapsed_time = 0
        while elapsed_time < timeout:
            try:
                status_data = await argo_connector.get_application_status(app_name)

                if not status_data:
                    logger.debug(f"Could not get status for '{app_name}', retrying...")
                    await asyncio.sleep(poll_interval)
                    elapsed_time += poll_interval
                    continue

                sync_status = status_data.get("status", {}).get("sync", {}).get("status")
                health_status = status_data.get("status", {}).get("health", {}).get("status")

                if sync_status == "Synced" and health_status == "Healthy":
                    logger.info(f"Application '{app_name}' is synced and healthy")
                    return True

                # Determine whether the status reflects our refresh.
                # If reconciledAt is still <= refreshed_after, ArgoCD
                # hasn't processed our changes yet - don't treat errors
                # as terminal.
                status_is_fresh = True
                if refreshed_after:
                    reconciled_at = status_data.get("status", {}).get("reconciledAt", "")
                    if reconciled_at <= refreshed_after:
                        status_is_fresh = False
                        logger.debug(
                            f"Application '{app_name}': status is stale "
                            f"(reconciledAt={reconciled_at} <= refreshed_after={refreshed_after}), "
                            f"not evaluating terminal states yet"
                        )

                if status_is_fresh:
                    # Check for terminal sync failures
                    operation_state = status_data.get("status", {}).get("operationState", {})
                    operation_phase = operation_state.get("phase")
                    if operation_phase in ("Failed", "Error"):
                        operation_message = operation_state.get("message", "")
                        error_msg = f"Application '{app_name}' sync failed: {operation_phase}"
                        if operation_message:
                            error_msg += f" - {operation_message}"
                        logger.error(error_msg)
                        raise RuntimeError(error_msg)

                    # Check for terminal health failure
                    if health_status == "Degraded":
                        error_msg = f"Application '{app_name}' is degraded"
                        logger.error(error_msg)
                        raise RuntimeError(error_msg)

                logger.debug(
                    f"Application '{app_name}': sync={sync_status}, health={health_status}, "
                    f"fresh={status_is_fresh}, waiting {poll_interval}s... (elapsed: {elapsed_time}s)"
                )
                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval

            except (RuntimeError, TimeoutError):
                raise
            except PermissionError:
                # Application may not be accessible yet (AppProject not synced)
                logger.debug(
                    f"Permission denied for '{app_name}', waiting for AppProject sync... (elapsed: {elapsed_time}s)"
                )
                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval
            except Exception as e:
                logger.warning(f"Error checking status of '{app_name}': {e}, retrying...")
                await asyncio.sleep(poll_interval)
                elapsed_time += poll_interval

        raise TimeoutError(f"Timeout waiting for application '{app_name}' to be synced and healthy after {timeout}s")
