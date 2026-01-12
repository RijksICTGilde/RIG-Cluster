"""
Centralized naming utilities for generating consistent unique names across the codebase.

This module provides standardized methods for generating unique names for Kubernetes resources
including deployments, services, PVCs, and other manifest resources.
"""

import re


def generate_unique_name(deployment_name: str, component_name: str) -> str:
    """
    Generate a unique name for Kubernetes resources using deployment and component names.

    This is the standard naming pattern used across all manifest resources within a namespace.
    The project name is not included since resources are deployed within project-specific namespaces.

    Args:
        deployment_name: Name of the deployment
        component_name: Name of the component

    Returns:
        Unique name in format: deployment-component

    Example:
        generate_unique_name("frontend", "webapp") -> "frontend-webapp"
    """
    return f"{deployment_name}-{component_name}"


def generate_storage_name(mount_path: str, index: int) -> str:
    """
    Generate a storage name based on mount path or index.

    This converts mount paths into valid Kubernetes resource names by removing
    invalid characters and providing fallback names.

    Args:
        mount_path: The mount path for the storage (e.g., "/data", "/app/logs")
        index: Index position as fallback if mount_path processing fails

    Returns:
        Storage name suitable for Kubernetes resources

    Example:
        generate_storage_name("/data", 0) -> "data"
        generate_storage_name("/app/logs", 1) -> "applogs"
        generate_storage_name("", 2) -> "storage2"
    """
    if not mount_path:
        return f"storage{index}"

    # Remove leading slash and replace invalid characters
    storage_name = mount_path.lstrip("/").replace("/", "").replace("-", "").replace("_", "")

    # Ensure the name is valid (lowercase alphanumeric)
    storage_name = re.sub(r"[^a-z0-9]", "", storage_name.lower())

    # Use fallback if processing results in empty string
    return storage_name or f"storage{index}"


def generate_pvc_name(unique_name: str, storage_name: str, generation: int = 0) -> str:
    """
    Generate a PVC name using the unique resource name and storage name with optional generation.

    Args:
        unique_name: The unique name for the resource (from generate_unique_name)
        storage_name: The storage name (from generate_storage_name)
        generation: Generation number for PVC recreation (0 = no suffix, for backward compatibility)

    Returns:
        PVC name in format:
        - generation 0: unique_name-storage_name-pvc
        - generation > 0: unique_name-storage_name-pvc-v{generation}

    Example:
        generate_pvc_name("frontend-webapp", "data") -> "frontend-webapp-data-pvc"
        generate_pvc_name("frontend-webapp", "data", 1) -> "frontend-webapp-data-pvc-v1"
        generate_pvc_name("frontend-webapp", "data", 2) -> "frontend-webapp-data-pvc-v2"
    """
    base_name = f"{unique_name}-{storage_name}-pvc"
    if generation > 0:
        return f"{base_name}-v{generation}"
    return base_name


def generate_manifest_name(component_name: str, manifest_type: str, generation: int = 0) -> str:
    """
    Generate a manifest filename that includes the component name for uniqueness.

    Args:
        component_name: Name of the component
        manifest_type: Type of manifest (e.g., "deployment", "service", "data-pvc")
        generation: Optional generation number for versioned resources (0 = no suffix, for backward compatibility)

    Returns:
        Unique manifest name in format:
        - generation 0: component-manifest_type
        - generation > 0: component-manifest_type-v{generation}

    Example:
        generate_manifest_name("webapp", "deployment") -> "webapp-deployment"
        generate_manifest_name("webapp", "data-pvc") -> "webapp-data-pvc"
        generate_manifest_name("webapp", "data-pvc", 1) -> "webapp-data-pvc-v1"
        generate_manifest_name("webapp", "data-pvc", 2) -> "webapp-data-pvc-v2"
    """
    base_name = f"{component_name}-{manifest_type}"
    if generation > 0:
        return f"{base_name}-v{generation}"
    return base_name


def generate_pvc_manifest_type(storage_name: str) -> str:
    """
    Generate the manifest type string for PVC resources.

    This centralizes the naming pattern for PVC manifest types to ensure
    consistency across manifest generation and cleanup operations.

    Args:
        storage_name: The storage name (from generate_storage_name)

    Returns:
        Manifest type string in format: {storage_name}-pvc

    Example:
        generate_pvc_manifest_type("data") -> "data-pvc"
        generate_pvc_manifest_type("applogs") -> "applogs-pvc"
    """
    return f"{storage_name}-pvc"


def sanitize_kubernetes_name(name: str, max_length: int = 63) -> str:
    """
    Sanitize a string to be a valid Kubernetes resource name.

    Kubernetes names must:
    - Be lowercase
    - Contain only alphanumeric characters and hyphens
    - Start and end with alphanumeric characters
    - Be no longer than 63 characters

    Args:
        name: The name to sanitize
        max_length: Maximum length for the name (default: 63)

    Returns:
        Sanitized name suitable for Kubernetes resources
    """
    if not name:
        return "unnamed"

    # Convert to lowercase and replace invalid characters with hyphens
    sanitized = re.sub(r"[^a-z0-9-]", "-", name.lower())

    # Remove leading/trailing hyphens and consecutive hyphens
    sanitized = re.sub(r"^-+|-+$", "", sanitized)
    sanitized = re.sub(r"-+", "-", sanitized)

    # Truncate if too long
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length].rstrip("-")

    # Ensure it's not empty after sanitization
    return sanitized or "unnamed"


def generate_hostname(component_name: str, deployment_name: str, project_name: str, ingress_postfix: str) -> str:
    """
    Generate a hostname for ingress based on component, deployment, project names and cluster configuration.

    Args:
        component_name: Name of the component
        deployment_name: Name of the deployment
        project_name: Name of the project
        ingress_postfix: Cluster-specific ingress postfix

    Returns:
        Hostname in format: component-deployment-project{ingress_postfix}

    Example:
        generate_hostname("webapp", "frontend", "myproject", ".dev.example.com")
        -> "webapp-frontend-myproject.dev.example.com"
    """
    return f"{component_name}-{deployment_name}-{project_name}{ingress_postfix}"


def generate_ingress_map(
    component_name: str, deployment_name: str, project_name: str, ingress_postfix: str, subdomain: str | None = None
) -> dict[str, str]:
    """
    Generate a map of ingress names to hostnames for a component.

    Supports 3 domain modes:
    1. Component-specific (subdomain=None): Each component gets unique URL
       -> component-deployment-project.domain
    2. Deployment-name mode (subdomain=deployment_name): Components share deployment-based domain
       -> deploymentname-project.domain (same for all components in deployment)
    3. Custom subdomain mode (subdomain!=deployment_name): Components share custom domain
       -> customsubdomain.domain (same for all components in deployment)

    Args:
        component_name: Name of the component
        deployment_name: Name of the deployment
        project_name: Name of the project
        ingress_postfix: Cluster-specific ingress postfix (e.g., ".dev.example.com")
        subdomain: Optional subdomain for shared domain mode (either deployment name or custom)

    Returns:
        Dictionary mapping ingress names to hostnames

    Examples:
        # Component-specific mode (no subdomain)
        generate_ingress_map("webapp", "main", "myproject", ".dev.example.com", None)
        -> {"main-webapp": "webapp-main-myproject.dev.example.com"}

        # Deployment-name mode (subdomain matches deployment name)
        generate_ingress_map("webapp", "main", "myproject", ".dev.example.com", "main")
        -> {"main-webapp": "main-myproject.dev.example.com"}

        # Custom subdomain mode (subdomain is custom value)
        generate_ingress_map("webapp", "main", "myproject", ".dev.example.com", "myapp")
        -> {"main-webapp": "myapp.dev.example.com"}
    """
    # Generate the base unique name for the resource
    base_name = generate_unique_name(deployment_name, component_name)

    # Determine hostname based on domain mode
    if subdomain:
        # Shared domain mode: distinguish between deployment-name and custom subdomain
        domain = ingress_postfix.lstrip(".")

        # If subdomain matches deployment_name, it's deployment-name mode -> include project name
        if subdomain == deployment_name:
            hostname = f"{subdomain}-{project_name}.{domain}"
        else:
            # Custom subdomain mode -> use subdomain as-is without project name
            hostname = f"{subdomain}.{domain}"
    else:
        # Component-specific mode: each component gets unique URL
        hostname = generate_hostname(component_name, deployment_name, project_name, ingress_postfix)

    return {base_name: hostname}


# Simple resource naming utilities


def generate_resource_identifier(project_name: str, postfix: str, separator: str = "_", max_length: int = 63) -> str:
    """
    Generate a consistent resource identifier by combining project name with a postfix.

    This is the core naming pattern used throughout OPI for database usernames, schemas,
    MinIO usernames, bucket names, etc. The project_manager determines the appropriate postfix
    (deployment_name, component_name, or combination) based on resource scope.

    Args:
        project_name: Name of the project
        postfix: The postfix to append (typically deployment_name or deployment_component)
        separator: Character to use between parts ('_' for identifiers, '-' for names)
        max_length: Maximum length for the result (default: 63 for most systems)

    Returns:
        Resource identifier string

    Example:
        generate_resource_identifier("myproject", "frontend", "_") -> "myproject_frontend"
        generate_resource_identifier("myproject", "frontend", "-") -> "myproject-frontend"
    """
    # Clean the inputs - just lowercase
    project_clean = project_name.lower()
    postfix_clean = postfix.lower()

    # Combine with separator
    identifier = f"{project_clean}{separator}{postfix_clean}"

    # Final character normalization based on separator choice
    if separator == "_":
        identifier = identifier.replace("-", "_")
    elif separator == "-":
        identifier = identifier.replace("_", "-")

    # Truncate if needed
    if len(identifier) > max_length:
        original_identifier = identifier
        identifier = identifier[:max_length]
        import logging

        logger = logging.getLogger(__name__)
        logger.warning(
            f"Resource identifier truncated from '{original_identifier}' to '{identifier}' (max_length={max_length})"
        )

    return identifier


def _sanitize_for_identifier(value: str) -> str:
    """
    Sanitize a string for use in database/system identifiers.

    Replaces hyphens with underscores and ensures valid identifier format.

    Args:
        value: String to sanitize

    Returns:
        Sanitized string safe for use as identifier
    """
    return value.replace("-", "_").lower()


def _sanitize_for_lowercase(value: str) -> str:
    """
    Sanitize a string for use in lowercase-only contexts.

    Keeps hyphens but ensures lowercase.

    Args:
        value: String to sanitize

    Returns:
        Lowercase string with original separators
    """
    return value.lower()


def _truncate_if_needed(name: str, max_length: int) -> str:
    """
    Truncate name if it exceeds maximum length.

    Args:
        name: Name to potentially truncate
        max_length: Maximum allowed length

    Returns:
        Truncated name if needed
    """
    if len(name) <= max_length:
        return name
    return name[:max_length]


def generate_database_username(project_name: str, deployment_name: str) -> str:
    """
    Generate a consistent database username.

    Format: {project}_{deployment} (no proj_ prefix)

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        Database username string
    """
    project_clean = _sanitize_for_identifier(project_name)
    deployment_clean = _sanitize_for_identifier(deployment_name)
    username = f"{project_clean}_{deployment_clean}"
    return _truncate_if_needed(username, 63)  # PostgreSQL username limit


def generate_database_schema(project_name: str, deployment_name: str) -> str:
    """
    Generate a consistent database schema name.

    Format: {project}_{deployment} (no proj_ prefix)

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        Database schema name string
    """
    project_clean = _sanitize_for_identifier(project_name)
    deployment_clean = _sanitize_for_identifier(deployment_name)
    schema = f"{project_clean}_{deployment_clean}"
    return _truncate_if_needed(schema, 63)  # PostgreSQL schema limit


def generate_database_name(project_name: str, deployment_name: str) -> str:
    """
    Generate a consistent database name.

    Format: {project}_{deployment} (no proj_ prefix)

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        Database name string
    """
    project_clean = _sanitize_for_identifier(project_name)
    deployment_clean = _sanitize_for_identifier(deployment_name)
    database = f"{project_clean}_{deployment_clean}"
    return _truncate_if_needed(database, 63)  # PostgreSQL database limit


def generate_minio_username(project_name: str, deployment_name: str) -> str:
    """
    Generate a consistent MinIO username.

    Format: {project}_{deployment} (no proj_ prefix, underscore separated)

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        MinIO username string
    """
    project_clean = _sanitize_for_identifier(project_name)
    deployment_clean = _sanitize_for_identifier(deployment_name)
    username = f"{project_clean}_{deployment_clean}"
    return _truncate_if_needed(username, 63)  # MinIO username limit


def generate_bucket_name(project_name: str, deployment_name: str) -> str:
    """
    Generate a consistent S3/MinIO bucket name.

    Format: {project}-{deployment} (no proj_ prefix, hyphen separated, lowercase)

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        Bucket name string (lowercase with hyphens)
    """
    project_clean = _sanitize_for_lowercase(project_name)
    deployment_clean = _sanitize_for_lowercase(deployment_name)
    bucket = f"{project_clean}-{deployment_clean}"
    return _truncate_if_needed(bucket, 63)  # S3 bucket name limit


def generate_minio_policy_name(project_name: str, deployment_name: str) -> str:
    """
    Generate a consistent MinIO policy name.

    Format: {username}-{bucket}-policy
    This must match the connector's grant_bucket_access naming convention where
    policies are named using both username and bucket name.

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        MinIO policy name string

    Example:
        generate_minio_policy_name("amt-136", "deployment-1")
        -> "amt_136_deployment_1-amt-136-deployment-1-policy"
    """
    username = generate_minio_username(project_name, deployment_name)
    bucket = generate_bucket_name(project_name, deployment_name)
    policy_name = f"{username}-{bucket}-policy"
    return _truncate_if_needed(policy_name, 128)  # MinIO policy name limit


def generate_keycloak_client_id(project_name: str, deployment_name: str, component_name: str | None = None) -> str:
    """
    Generate a consistent Keycloak client ID.

    Format: {project}-{deployment}[-{component}] (no proj_ prefix)

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment
        component_name: Optional component name for component-specific clients

    Returns:
        Keycloak client ID string
    """
    project_clean = _sanitize_for_lowercase(project_name)
    deployment_clean = _sanitize_for_lowercase(deployment_name)

    if component_name:
        component_clean = _sanitize_for_lowercase(component_name)
        client_id = f"{project_clean}-{deployment_clean}-{component_clean}"
    else:
        client_id = f"{project_clean}-{deployment_clean}"

    return _truncate_if_needed(client_id, 255)  # Keycloak client ID limit


def generate_argocd_application_name(project_name: str, deployment_name: str) -> str:
    """
    Generate a consistent ArgoCD application name.

    Format: {project}-{deployment} (hyphen separated, lowercase)
    This is the name used within ArgoCD for the application resource.

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        ArgoCD application name string
    """
    project_clean = _sanitize_for_lowercase(project_name)
    deployment_clean = _sanitize_for_lowercase(deployment_name)
    app_name = f"{project_clean}-{deployment_clean}"
    return _truncate_if_needed(app_name, 253)  # ArgoCD application name limit


def generate_argocd_application_filename(project_name: str, deployment_name: str) -> str:
    """
    Generate a consistent ArgoCD application filename.

    Format: {project}-{deployment}-argocd-application.yaml
    This is the filename used for storing ArgoCD application manifests in git.

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        ArgoCD application filename
    """
    project_clean = _sanitize_for_lowercase(project_name)
    deployment_clean = _sanitize_for_lowercase(deployment_name)
    filename = f"{project_clean}-{deployment_clean}-argocd-application.yaml"
    return filename


def generate_gitops_manifests_folder_path(cluster: str, project_name: str, deployment_name: str) -> str:
    """
    Generate a consistent GitOps folder path for deployments.

    Format: {cluster}/{project}/{deployment}
    This is the folder structure used in the GitOps repository.

    Args:
        cluster: Name of the cluster
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        GitOps folder path
    """
    cluster_clean = _sanitize_for_lowercase(cluster)
    project_clean = _sanitize_for_lowercase(project_name)
    deployment_clean = _sanitize_for_lowercase(deployment_name)
    return f"{cluster_clean}/{project_clean}/{deployment_clean}"


def generate_gitops_argocd_application_path(cluster: str, project_name: str, deployment_name: str) -> str:
    """
    Generate the full path to the ArgoCD application file in the GitOps repository.

    Format: {cluster}/{project}/{project}-{deployment}-argocd-application.yaml

    Args:
        cluster: Name of the cluster
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        Full path to the ArgoCD application file
    """
    cluster_clean = _sanitize_for_lowercase(cluster)
    project_clean = _sanitize_for_lowercase(project_name)
    filename = generate_argocd_application_filename(project_name, deployment_name)
    return f"{cluster_clean}/{project_clean}/{filename}"


def generate_deployment_manifest_path(
    cluster: str, project_name: str, deployment_name: str, repo_path: str = ""
) -> str:
    """
    Generate the deployment manifest path in the application repository.

    Format: {repo_path}/{cluster}/{project}/{deployment}

    Args:
        cluster: Name of the cluster
        project_name: Name of the project
        deployment_name: Name of the deployment
        repo_path: Optional repository base path

    Returns:
        Deployment manifest path
    """
    cluster_clean = _sanitize_for_lowercase(cluster)
    project_clean = _sanitize_for_lowercase(project_name)
    deployment_clean = _sanitize_for_lowercase(deployment_name)

    if repo_path:
        return f"{repo_path}/{cluster_clean}/{project_clean}/{deployment_clean}"
    else:
        return f"{cluster_clean}/{project_clean}/{deployment_clean}"


def generate_project_deployment_prefix(project_name: str, deployment_name: str) -> str:
    """
    Generate a consistent project-deployment prefix for naming.

    Format: {project}-{deployment} (hyphen separated, lowercase)
    This prefix is used for various ArgoCD resources and filenames.

    Args:
        project_name: Name of the project
        deployment_name: Name of the deployment

    Returns:
        Project-deployment prefix string
    """
    project_clean = _sanitize_for_lowercase(project_name)
    deployment_clean = _sanitize_for_lowercase(deployment_name)
    return f"{project_clean}-{deployment_clean}"


def generate_infrastructure_namespace_base(project_name: str) -> str:
    """
    Generate base infrastructure namespace name (without cluster prefix).

    Format: {project}-infrastructure

    This is the base name that will be prefixed with cluster-specific prefix
    using get_prefixed_namespace() to create the final namespace name.

    Args:
        project_name: Name of the project

    Returns:
        Base infrastructure namespace name

    Example:
        generate_infrastructure_namespace_base("myproject") -> "myproject-infrastructure"
        Then use: get_prefixed_namespace("local", "myproject-infrastructure") -> "rig-myproject-infrastructure"
    """
    project_clean = _sanitize_for_lowercase(project_name)
    return f"{project_clean}-infrastructure"


def generate_argocd_appproject_prefix(project_name: str, namespace: str) -> str:
    """
    Generate a consistent project-namespace prefix for ArgoCD AppProject naming.

    Format: {project}-{namespace} (hyphen separated, lowercase)
    This prefix is used for both the manifest name and filename.
    AppProjects manage namespaces, so they're named by project + namespace.

    Args:
        project_name: Name of the project
        namespace: Target namespace (already prefixed with cluster)

    Returns:
        Project-namespace prefix string
    """
    project_clean = _sanitize_for_lowercase(project_name)
    namespace_clean = _sanitize_for_lowercase(namespace)
    return f"{project_clean}-{namespace_clean}"


def generate_argocd_repository_secret_name(
    project_name: str, repository_name: str, credential_hash: str | None = None
) -> str:
    """
    Generate a consistent name for ArgoCD repository secrets.

    Combines project name with repository name to create a unique identifier
    for the repository secret. The name is sanitized to comply with Kubernetes
    naming requirements.

    When credential_hash is provided, it's included in the name to create a
    version-specific identifier. This forces ArgoCD to create a new secret when
    credentials change, as the filename (which includes this name) will be different.

    Format: {project}-{repository}[-{hash}] (hyphen separated, lowercase, sanitized)

    Args:
        project_name: Name of the project
        repository_name: Name of the repository from project configuration
        credential_hash: Optional 8-character hash of credentials for versioning

    Returns:
        Sanitized secret name suitable for Kubernetes resources

    Example:
        >>> generate_argocd_repository_secret_name("my-project", "main-repo")
        'my-project-main-repo'
        >>> generate_argocd_repository_secret_name("MyProject", "Main_Repo")
        'myproject-main-repo'
        >>> generate_argocd_repository_secret_name("my-project", "main-repo", "a1b2c3d4")
        'my-project-main-repo-a1b2c3d4'
    """
    # Combine project and repository name
    combined = f"{project_name}-{repository_name}"

    # Add credential hash if provided for versioning
    if credential_hash:
        combined = f"{combined}-{credential_hash}"

    # Sanitize to ensure Kubernetes compliance
    return sanitize_kubernetes_name(combined)


def get_output_filename_from_template(template_filename: str, prefix: str = "") -> str:
    """
    Convert a Jinja2 template filename to the corresponding output filename.

    Args:
        template_filename: Template filename (e.g., "argocd-application.yaml.jinja")
        prefix: Optional prefix to add to the output filename

    Returns:
        Output filename (e.g., "my-app-argocd-application.yaml")
    """
    # Remove .jinja extension if present
    base_filename = template_filename[:-6] if template_filename.endswith(".jinja") else template_filename
    # Add prefix if provided
    return f"{prefix}-{base_filename}" if prefix else base_filename


def generate_public_url(hostname: str, use_https: bool = True) -> str:
    """
    Generate a full public URL from a hostname.

    This function provides consistent URL generation across the application,
    following the convention of using HTTPS for public ingress endpoints.

    Args:
        hostname: The hostname (e.g., "webapp-frontend-myproject.dev.example.com")
        use_https: Whether to use HTTPS protocol (default: True)

    Returns:
        Full URL string

    Example:
        generate_public_url("webapp-frontend-myproject.dev.example.com")
        -> "https://webapp-frontend-myproject.dev.example.com"
    """
    protocol = "https" if use_https else "http"
    return f"{protocol}://{hostname}"


def make_argocd_repository_url_unique(repo_url: str, project_name: str) -> str:
    """
    Make a repository URL unique for ArgoCD by embedding project name as username.

    ArgoCD uses the full URL string as a cache key for repository credentials.
    By adding the project name as a username, each project gets a unique URL while
    Git operations remain unaffected (username is ignored for token authentication).

    This function safely handles URLs that already contain usernames by replacing them.
    It only modifies HTTPS URLs from known Git hosting services.

    Args:
        repo_url: Original repository URL
        project_name: Project name to use as unique identifier

    Returns:
        Modified URL with project name as username, or original URL if not applicable

    Examples:
        >>> make_argocd_repository_url_unique(
        ...     "https://github.com/org/repo.git",
        ...     "project-a"
        ... )
        'https://project-a@github.com/org/repo.git'

        >>> make_argocd_repository_url_unique(
        ...     "https://existing@github.com/org/repo.git",
        ...     "project-b"
        ... )
        'https://project-b@github.com/org/repo.git'

        >>> make_argocd_repository_url_unique(
        ...     "ssh://git@github.com/org/repo.git",
        ...     "project-c"
        ... )
        'ssh://git@github.com/org/repo.git'  # SSH URLs unchanged
    """
    from urllib.parse import urlparse, urlunparse

    # Only modify HTTPS URLs
    if not repo_url.startswith("https://"):
        return repo_url

    # Only modify known Git hosting services to avoid breaking custom URLs
    known_hosts = ["github.com", "gitlab.com", "bitbucket.org"]
    if not any(host in repo_url for host in known_hosts):
        return repo_url

    # Parse the URL
    parsed = urlparse(repo_url)

    # Build netloc with project name as username (replaces existing username if present)
    netloc = f"{project_name}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"

    # Reconstruct URL with new username
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def generate_project_admin_username(project_name: str, cluster: str) -> str:
    """
    Generate a consistent project admin username.

    Format: {project}_{cluster}_admin (underscore separated for identifier)
    This is the username for the project administrator in the master realm.

    Args:
        project_name: Name of the project
        cluster: Name of the cluster

    Returns:
        Project admin username string

    Example:
        generate_project_admin_username("myproject", "production")
        -> "myproject_production_admin"
    """
    project_clean = _sanitize_for_identifier(project_name)
    cluster_clean = _sanitize_for_identifier(cluster)
    username = f"{project_clean}_{cluster_clean}_admin"
    return _truncate_if_needed(username, 63)


def generate_project_realm_name(project_name: str, cluster: str) -> str:
    """
    Generate a consistent project realm name.

    Format: {project}-{cluster} (hyphen separated, lowercase)
    This is the name of the realm created for the project in Keycloak.

    Args:
        project_name: Name of the project
        cluster: Name of the cluster

    Returns:
        Project realm name string

    Example:
        generate_project_realm_name("myproject", "production")
        -> "myproject-production"
    """
    project_clean = _sanitize_for_lowercase(project_name)
    cluster_clean = _sanitize_for_lowercase(cluster)
    realm = f"{project_clean}-{cluster_clean}"
    return _truncate_if_needed(realm, 255)


def generate_project_platform_client_id(project_name: str, cluster: str) -> str:
    """
    Generate a consistent client ID for the project's platform client.

    Format: {project}-{cluster}-platform (hyphen separated, lowercase)
    This is the client ID in the RIG Platform realm that federates to the project realm.

    Args:
        project_name: Name of the project
        cluster: Name of the cluster

    Returns:
        Platform client ID string

    Example:
        generate_project_platform_client_id("myproject", "production")
        -> "myproject-production-platform"
    """
    project_clean = _sanitize_for_lowercase(project_name)
    cluster_clean = _sanitize_for_lowercase(cluster)
    client_id = f"{project_clean}-{cluster_clean}-platform"
    return _truncate_if_needed(client_id, 255)


def generate_postgres_superuser_secret_name(project_name: str) -> str:
    """
    Generate the secret name for namespace-specific PostgreSQL superuser credentials.

    Format: {project}-postgres-superuser (hyphen separated, lowercase)
    This secret is stored in the infrastructure namespace and contains
    the superuser credentials for the project's dedicated PostgreSQL cluster.

    Args:
        project_name: Name of the project

    Returns:
        Secret name string

    Example:
        generate_postgres_superuser_secret_name("myproject")
        -> "myproject-postgres-superuser"
    """
    project_clean = _sanitize_for_lowercase(project_name)
    return f"{project_clean}-postgres-superuser"


def generate_infrastructure_application_name(project_name: str) -> str:
    """
    Generate the ArgoCD application name for project infrastructure.

    Format: {project}-infrastructure (hyphen separated, lowercase)
    This application manages the infrastructure resources like PostgreSQL clusters.

    Args:
        project_name: Name of the project

    Returns:
        Infrastructure application name string

    Example:
        generate_infrastructure_application_name("myproject")
        -> "myproject-infrastructure"
    """
    project_clean = _sanitize_for_lowercase(project_name)
    return f"{project_clean}-infrastructure"


def generate_infrastructure_manifest_path(cluster: str, project_name: str, repo_path: str = "") -> str:
    """
    Generate the path to infrastructure manifests in the deployment git repository.

    Format: {repo_path}/{cluster}/{project}/infrastructure (if repo_path provided)
            {cluster}/{project}/infrastructure (if no repo_path)

    Args:
        cluster: Name of the cluster
        project_name: Name of the project
        repo_path: Optional repository base path

    Returns:
        Infrastructure manifest path string

    Example:
        generate_infrastructure_manifest_path("production", "myproject")
        -> "production/myproject/infrastructure"

        generate_infrastructure_manifest_path("production", "myproject", "deployments")
        -> "deployments/production/myproject/infrastructure"
    """
    project_clean = _sanitize_for_lowercase(project_name)
    cluster_clean = _sanitize_for_lowercase(cluster)

    if repo_path:
        return f"{repo_path}/{cluster_clean}/{project_clean}/infrastructure"
    return f"{cluster_clean}/{project_clean}/infrastructure"


def generate_infrastructure_argocd_application_filename(project_name: str) -> str:
    """
    Generate the ArgoCD application filename for project infrastructure.

    Format: {project}-infrastructure-argocd-application.yaml

    Args:
        project_name: Name of the project

    Returns:
        Infrastructure ArgoCD application filename

    Example:
        generate_infrastructure_argocd_application_filename("myproject")
        -> "myproject-infrastructure-argocd-application.yaml"
    """
    project_clean = _sanitize_for_lowercase(project_name)
    return f"{project_clean}-infrastructure-argocd-application.yaml"


def generate_infrastructure_argocd_appproject_filename(project_name: str) -> str:
    """
    Generate the ArgoCD AppProject filename for project infrastructure.

    Format: {project}-infrastructure-argocd-appproject.yaml

    Args:
        project_name: Name of the project

    Returns:
        Infrastructure ArgoCD AppProject filename

    Example:
        generate_infrastructure_argocd_appproject_filename("myproject")
        -> "myproject-infrastructure-argocd-appproject.yaml"
    """
    project_clean = _sanitize_for_lowercase(project_name)
    return f"{project_clean}-infrastructure-argocd-appproject.yaml"


def generate_infrastructure_argocd_folder_path(cluster: str, project_name: str) -> str:
    """
    Generate the folder path for infrastructure ArgoCD resources in the GitOps repository.

    Format: {cluster}/{project}-infrastructure/

    Args:
        cluster: Name of the cluster
        project_name: Name of the project

    Returns:
        Infrastructure ArgoCD folder path

    Example:
        generate_infrastructure_argocd_folder_path("production", "myproject")
        -> "production/myproject-infrastructure"
    """
    cluster_clean = _sanitize_for_lowercase(cluster)
    project_clean = _sanitize_for_lowercase(project_name)
    return f"{cluster_clean}/{project_clean}-infrastructure"


def generate_infrastructure_argocd_application_path(cluster: str, project_name: str) -> str:
    """
    Generate the full path to the infrastructure ArgoCD application file in the GitOps repository.

    Format: {cluster}/{project}-infrastructure/{project}-infrastructure-argocd-application.yaml

    Args:
        cluster: Name of the cluster
        project_name: Name of the project

    Returns:
        Full path to the infrastructure ArgoCD application file

    Example:
        generate_infrastructure_argocd_application_path("production", "myproject")
        -> "production/myproject-infrastructure/myproject-infrastructure-argocd-application.yaml"
    """
    folder_path = generate_infrastructure_argocd_folder_path(cluster, project_name)
    filename = generate_infrastructure_argocd_application_filename(project_name)
    return f"{folder_path}/{filename}"


def generate_registry_secret_name(deployment_name: str, registry_name: str) -> str:
    """
    Generate registry secret name for a deployment and registry combination.

    Args:
        deployment_name: Name of the deployment
        registry_name: Name of the registry (will be normalized)

    Returns:
        Registry secret name in format: {deployment}-{normalized-registry}-secret

    Example:
        >>> generate_registry_secret_name("frontend", "github-registry")
        'frontend-github-registry-secret'
    """
    normalized_registry = _sanitize_for_lowercase(registry_name)
    return f"{deployment_name}-{normalized_registry}-secret"


def generate_tls_secret_name(ingress_name: str) -> str:
    """
    Generate a consistent TLS secret name for an ingress resource.

    The secret name follows the pattern {ingress_name}-tls, which cert-manager
    uses to store the generated TLS certificate and key.

    Args:
        ingress_name: Name of the ingress resource

    Returns:
        TLS secret name string

    Example:
        >>> generate_tls_secret_name("main-webapp")
        'main-webapp-tls'
        >>> generate_tls_secret_name("frontend-api-v1users")
        'frontend-api-v1users-tls'
    """
    return f"{ingress_name}-tls"


def normalize_base_domain(base_domain: str, max_length: int = 50) -> str:
    """
    Normalize a base domain for use in Kubernetes resource names.

    Converts domain names to valid Kubernetes name components by replacing
    dots with hyphens and ensuring lowercase.

    Args:
        base_domain: The domain to normalize (e.g., "rijksapp.com")
        max_length: Maximum length for the normalized string (default: 50 to leave room for prefixes)

    Returns:
        Normalized domain string suitable for Kubernetes names

    Example:
        >>> normalize_base_domain("rijksapp.com")
        'rijksapp-com'
        >>> normalize_base_domain("my.subdomain.example.org")
        'my-subdomain-example-org'
    """
    if not base_domain:
        return ""

    # Replace dots with hyphens and lowercase
    normalized = base_domain.lower().replace(".", "-")

    # Remove any other invalid characters
    normalized = re.sub(r"[^a-z0-9-]", "", normalized)

    # Remove leading/trailing hyphens and consecutive hyphens
    normalized = re.sub(r"^-+|-+$", "", normalized)
    normalized = re.sub(r"-+", "-", normalized)

    # Truncate if needed
    if len(normalized) > max_length:
        normalized = normalized[:max_length].rstrip("-")

    return normalized


def generate_issuer_name(base_domain: str, issuer_type: str = "letsencrypt") -> str:
    """
    Generate a consistent cert-manager Issuer name for a base domain.

    The issuer name includes the issuer type prefix and normalized domain
    to ensure uniqueness per domain within a namespace.

    Args:
        base_domain: The base domain (e.g., "rijksapp.com")
        issuer_type: The issuer type ("letsencrypt" or "letsencrypt-staging")

    Returns:
        Issuer name string

    Example:
        >>> generate_issuer_name("rijksapp.com")
        'letsencrypt-rijksapp-com'
        >>> generate_issuer_name("rijksapp.com", "letsencrypt-staging")
        'letsencrypt-staging-rijksapp-com'
    """
    normalized = normalize_base_domain(base_domain)
    return sanitize_kubernetes_name(f"{issuer_type}-{normalized}")


def generate_issuer_secret_name(base_domain: str, issuer_type: str = "letsencrypt") -> str:
    """
    Generate a consistent ACME account private key secret name for an Issuer.

    This secret stores the Let's Encrypt account private key used for ACME challenges.

    Args:
        base_domain: The base domain (e.g., "rijksapp.com")
        issuer_type: The issuer type ("letsencrypt" or "letsencrypt-staging")

    Returns:
        Secret name string

    Example:
        >>> generate_issuer_secret_name("rijksapp.com")
        'letsencrypt-rijksapp-com-key'
        >>> generate_issuer_secret_name("rijksapp.com", "letsencrypt-staging")
        'letsencrypt-staging-rijksapp-com-key'
    """
    issuer_name = generate_issuer_name(base_domain, issuer_type)
    return f"{issuer_name}-key"


def generate_issuer_manifest_name(base_domain: str, issuer_type: str = "letsencrypt") -> str:
    """
    Generate a consistent filename for the Issuer manifest.

    Args:
        base_domain: The base domain (e.g., "rijksapp.com")
        issuer_type: The issuer type ("letsencrypt" or "letsencrypt-staging")

    Returns:
        Manifest filename string

    Example:
        >>> generate_issuer_manifest_name("rijksapp.com")
        'issuer-letsencrypt-rijksapp-com.yaml'
        >>> generate_issuer_manifest_name("rijksapp.com", "letsencrypt-staging")
        'issuer-letsencrypt-staging-rijksapp-com.yaml'
    """
    issuer_name = generate_issuer_name(base_domain, issuer_type)
    return f"issuer-{issuer_name}.yaml"


def generate_network_policy_name(purpose: str) -> str:
    """
    Generate a consistent name for a NetworkPolicy.

    Args:
        purpose: The purpose of the network policy (e.g., "acme-http")

    Returns:
        NetworkPolicy name string

    Example:
        >>> generate_network_policy_name("acme-http")
        'acme-http-network-policy'
    """
    return f"{purpose}-network-policy"


def generate_network_policy_manifest_name(purpose: str) -> str:
    """
    Generate a consistent filename for a NetworkPolicy manifest.

    Args:
        purpose: The purpose of the network policy (e.g., "acme-http")

    Returns:
        Manifest filename string (without .yaml extension)

    Example:
        >>> generate_network_policy_manifest_name("acme-http")
        'acme-http-network-policy'
    """
    return generate_network_policy_name(purpose)


def resolve_effective_base_domain(base_domain: str | None, ingress_postfix: str) -> str:
    """
    Resolve the effective base domain, falling back to cluster ingress postfix.

    When a deployment specifies a base-domain, use it. Otherwise, fall back to
    the cluster's ingress_postfix (stripped of leading dot).

    Args:
        base_domain: Optional explicit base domain from deployment config
        ingress_postfix: Cluster's ingress postfix (e.g., ".kind", ".local")

    Returns:
        Effective base domain to use for hostname generation

    Example:
        >>> resolve_effective_base_domain("rijksapp.com", ".kind")
        'rijksapp.com'
        >>> resolve_effective_base_domain(None, ".kind")
        'kind'
        >>> resolve_effective_base_domain("", ".local")
        'local'
    """
    if base_domain:
        return base_domain
    # Strip leading dot from ingress_postfix
    return ingress_postfix.lstrip(".")


def generate_external_hostname(subdomain: str, base_domain: str) -> str:
    """
    Generate a hostname for external domain access.

    Combines subdomain and base domain to create the full hostname.

    Args:
        subdomain: The subdomain part (e.g., "myapp")
        base_domain: The base domain (e.g., "rijksapp.com")

    Returns:
        Full hostname string

    Example:
        >>> generate_external_hostname("myapp", "rijksapp.com")
        'myapp.rijksapp.com'
    """
    return f"{subdomain}.{base_domain}"


def generate_helm_values_filename(deployment_name: str, chart_name: str, encrypted: bool = True) -> str:
    """
    Generate a consistent filename for Helm values files.

    The naming convention allows the CMP to identify and decrypt SOPS-encrypted
    Helm values files before passing them to kustomize build. The filename includes
    both deployment and chart name since values are merged per deployment.

    Args:
        deployment_name: Name of the deployment
        chart_name: Name of the helm chart
        encrypted: Whether to generate the encrypted (.sops.yaml) or decrypted (.yaml) filename

    Returns:
        Helm values filename string

    Examples:
        >>> generate_helm_values_filename("local-deployment", "docs")
        'local-deployment-docs-helm-values.sops.yaml'
        >>> generate_helm_values_filename("local-deployment", "docs", encrypted=False)
        'local-deployment-docs-helm-values.yaml'
    """
    extension = ".sops.yaml" if encrypted else ".yaml"
    deployment_clean = _sanitize_for_lowercase(deployment_name)
    chart_clean = _sanitize_for_lowercase(chart_name)
    return f"{deployment_clean}-{chart_clean}-helm-values{extension}"


def generate_ingress_name_from_path(base_name: str, path: str, max_length: int = 63) -> str:
    """
    Generate an ingress resource name that includes the path for uniqueness.

    When a component exposes multiple paths, each path needs its own Ingress resource
    with a unique name. This function creates that unique name by appending a
    normalized version of the path to the base resource name.

    Args:
        base_name: Base resource name (e.g., "deployment-component")
        path: The URL path (e.g., "/api", "/v1/users")
        max_length: Maximum length for Kubernetes names (default: 63)

    Returns:
        Unique ingress name suitable for Kubernetes resources

    Examples:
        >>> generate_ingress_name_from_path("main-api", "/")
        'main-api'
        >>> generate_ingress_name_from_path("main-api", "/api")
        'main-api-api'
        >>> generate_ingress_name_from_path("main-api", "/v1/users")
        'main-api-v1users'
        >>> generate_ingress_name_from_path("main-api", "/health-check")
        'main-api-healthcheck'
    """
    # Root path doesn't add suffix
    if path == "/" or not path:
        return sanitize_kubernetes_name(base_name, max_length)

    # Normalize path: remove leading slash, replace / with empty, lowercase
    path_suffix = path.lstrip("/").replace("/", "").lower()
    path_suffix = re.sub(r"[^a-z0-9]", "", path_suffix)

    if not path_suffix:
        return sanitize_kubernetes_name(base_name, max_length)

    return sanitize_kubernetes_name(f"{base_name}-{path_suffix}", max_length)
