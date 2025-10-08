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


def generate_pvc_name(unique_name: str, storage_name: str) -> str:
    """
    Generate a PVC name using the unique resource name and storage name.

    Args:
        unique_name: The unique name for the resource (from generate_unique_name)
        storage_name: The storage name (from generate_storage_name)

    Returns:
        PVC name in format: unique_name-storage_name-pvc

    Example:
        generate_pvc_name("frontend-webapp", "data") -> "frontend-webapp-data-pvc"
    """
    return f"{unique_name}-{storage_name}-pvc"


def generate_manifest_name(component_name: str, manifest_type: str) -> str:
    """
    Generate a manifest filename that includes the component name for uniqueness.

    Args:
        component_name: Name of the component
        manifest_type: Type of manifest (e.g., "deployment", "service", "ingress")

    Returns:
        Unique manifest name in format: component-manifest_type

    Example:
        generate_manifest_name("webapp", "deployment") -> "webapp-deployment"
    """
    return f"{component_name}-{manifest_type}"


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

    Creates ingresses for:
    - Default: standard project naming convention
    - Subdomain: custom subdomain if specified in deployment

    Args:
        component_name: Name of the component
        deployment_name: Name of the deployment
        project_name: Name of the project
        ingress_postfix: Cluster-specific ingress postfix (e.g., ".dev.example.com")
        subdomain: Optional subdomain for additional ingress

    Returns:
        Dictionary mapping ingress names to hostnames

    Example:
        generate_ingress_map("webapp", "frontend", "myproject", ".dev.example.com", "api")
        -> {
            "frontend-webapp": "webapp-frontend-myproject.dev.example.com",
            "frontend-webapp-subdomain": "api.dev.example.com"
        }
    """
    # Generate the base unique name for the resource
    base_name = generate_unique_name(deployment_name, component_name)

    # Default ingress with standard naming
    default_hostname = generate_hostname(component_name, deployment_name, project_name, ingress_postfix)

    ingress_map = {base_name: default_hostname}

    # Add subdomain ingress if subdomain is specified
    if subdomain:
        # Extract domain from ingress_postfix (remove leading dot if present)
        domain = ingress_postfix.lstrip(".")
        subdomain_hostname = f"{subdomain}.{domain}"
        subdomain_ingress_name = f"{base_name}-subdomain"

        ingress_map[subdomain_ingress_name] = subdomain_hostname

    return ingress_map


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


def generate_keycloak_client_id(project_name: str, deployment_name: str, component_name: str = None) -> str:
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
