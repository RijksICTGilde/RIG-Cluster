"""
Cluster configuration for different environments.

This module defines cluster-specific settings including ingress postfixes.
"""

# TODO: In the future, read this configuration from YAML file
CLUSTER_CONFIG = {
    "local": {
        "ingress_postfix": ".kind",
        "namespace_prefix": "rig-",
        "argo_namespace": "rig-system",
        "namespace": "rig-system",
        "keycloak_discovery_url": "http://keycloak.kind",  # For pods in cluster
        "database_server": "rig-db-rw.rig-system.svc.cluster.local",
        "minio_server": "minio.rig-system.svc.cluster.local:9000",
        "ingress": {"enable_tls": False, "ip_whitelist": "0.0.0.0"},
        "storage": {"storage_class_name": "standard", "access_modes": ["ReadWriteOnce"]},
        "keycloak": {
            "support_http": True,  # Generate both HTTP and HTTPS redirect URIs
        },
    },
    "odcn-production": {
        "ingress_postfix": ".rig.prd1.gn2.quattro.rijksapps.nl",
        "namespace_prefix": "rig-prd-",
        "namespace": "rig-prd-operations",
        "argo_namespace": "rig-prd-operations",
        "keycloak_discovery_url": "https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl",  # For pods in cluster
        "database_server": "rig-db-rw.rig-prd-operations.svc.cluster.local",  # Assuming production DB is in operations namespace
        "minio_server": "minio.rig-prd-operations.svc.cluster.local:9000",
        "ingress": {
            "enable_tls": True,
            "ip_whitelist": "0.0.0.0/0",  # VPN only: "147.181.0.0/16"
        },
        "storage": {"storage_class_name": "ocs-storagecluster-ceph-rbd", "access_modes": ["ReadWriteOnce"]},
        "keycloak": {
            "support_http": False,  # Only generate HTTPS redirect URIs in production
        },
    },
}


def get_cluster_config(cluster_name: str) -> dict:
    """
    Get configuration for a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Dictionary containing cluster configuration

    Raises:
        ValueError: If cluster is not found in configuration
    """
    if cluster_name not in CLUSTER_CONFIG:
        raise ValueError(f"Cluster '{cluster_name}' not found in configuration")

    return CLUSTER_CONFIG[cluster_name]


def get_ingress_postfix(cluster_name: str) -> str:
    """
    Get the ingress postfix for a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Ingress postfix string

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["ingress_postfix"]


def get_namespace_prefix(cluster_name: str) -> str:
    """
    Get the namespace prefix for a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Namespace prefix string

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["namespace_prefix"]


def get_argo_namespace(cluster_name: str) -> str:
    """
    Get the ArgoCD namespace for a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        ArgoCD namespace string

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["argo_namespace"]


def get_prefixed_namespace(cluster_name: str, namespace: str) -> str:
    """
    Get a namespace with the appropriate prefix for a specific cluster.

    Args:
        cluster_name: Name of the cluster
        namespace: The base namespace name

    Returns:
        Namespace with cluster-specific prefix

    Raises:
        ValueError: If cluster is not found in configuration
    """
    prefix = get_namespace_prefix(cluster_name)
    return f"{prefix}{namespace}"


def get_storage_config(cluster_name: str) -> dict:
    """
    Get the storage configuration for a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Dictionary containing storage configuration

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("storage", {"storage_class_name": "standard", "access_modes": ["ReadWriteOnce"]})


def get_storage_class_name(cluster_name: str) -> str:
    """
    Get the default storage class name for a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Storage class name string

    Raises:
        ValueError: If cluster is not found in configuration
    """
    storage_config = get_storage_config(cluster_name)
    return storage_config["storage_class_name"]


def get_storage_access_modes(cluster_name: str) -> list[str]:
    """
    Get the default access modes for storage in a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        List of access mode strings

    Raises:
        ValueError: If cluster is not found in configuration
    """
    storage_config = get_storage_config(cluster_name)
    return storage_config["access_modes"]


def get_ingress_config(cluster_name: str) -> dict:
    """
    Get the ingress configuration for a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Dictionary containing ingress configuration

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("ingress", {"enable_tls": False, "ip_whitelist": "0.0.0.0"})


def get_ingress_tls_enabled(cluster_name: str) -> bool:
    """
    Check if TLS is enabled for ingresses in a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        True if TLS is enabled, False otherwise

    Raises:
        ValueError: If cluster is not found in configuration
    """
    ingress_config = get_ingress_config(cluster_name)
    return ingress_config["enable_tls"]


def get_ingress_ip_whitelist(cluster_name: str) -> str:
    """
    Get the IP whitelist for ingresses in a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        IP whitelist string (CIDR format)

    Raises:
        ValueError: If cluster is not found in configuration
    """
    ingress_config = get_ingress_config(cluster_name)
    return ingress_config["ip_whitelist"]


def get_keycloak_discovery_url(cluster_name: str) -> str:
    """
    Get the Keycloak discovery URL for pods in a specific cluster.

    This is the URL that pods will use to connect to Keycloak internally.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Keycloak discovery URL string for internal pod use

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["keycloak_discovery_url"]


def get_keycloak_config(cluster_name: str) -> dict:
    """
    Get the Keycloak configuration for a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Dictionary containing Keycloak configuration

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("keycloak", {"support_http": False})


def get_keycloak_support_http(cluster_name: str) -> bool:
    """
    Check if HTTP redirect URIs should be generated for Keycloak clients.

    Args:
        cluster_name: Name of the cluster

    Returns:
        True if both HTTP and HTTPS redirect URIs should be generated, False for HTTPS only

    Raises:
        ValueError: If cluster is not found in configuration
    """
    keycloak_config = get_keycloak_config(cluster_name)
    return keycloak_config.get("support_http", False)


def get_database_server(cluster_name: str) -> str:
    """
    Get the database server hostname for pods in a specific cluster.

    This is the hostname that pods will use to connect to the PostgreSQL database internally.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Database server hostname string for internal pod use

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["database_server"]


def get_minio_server(cluster_name: str) -> str:
    """
    Get the minio server hostname for pods in a specific cluster.

    This is the hostname that pods will use to connect to the minio internally.

    Args:
        cluster_name: Name of the cluster

    Returns:
        minio server hostname string for internal pod use

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["minio_server"]


def get_namespace(cluster_name: str) -> str:
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["namespace"]


def get_infrastructure_namespace(cluster_name: str, project_name: str) -> str:
    """
    Get infrastructure namespace with cluster-specific prefix.

    This combines the base infrastructure namespace name with the cluster's
    namespace prefix to create the final namespace name.

    Args:
        cluster_name: Name of the cluster (e.g., "local", "odcn-production")
        project_name: Name of the project

    Returns:
        Infrastructure namespace with cluster prefix

    Examples:
        get_infrastructure_namespace("local", "myproject")
        -> "rig-myproject-infrastructure"

        get_infrastructure_namespace("odcn-production", "myproject")
        -> "rig-prd-myproject-infrastructure"
    """
    from opi.utils.naming import generate_infrastructure_namespace_base

    base_name = generate_infrastructure_namespace_base(project_name)
    return get_prefixed_namespace(cluster_name, base_name)


def get_database_cluster_service_endpoint(cluster_name: str, project_name: str) -> str:
    """
    Get full database service endpoint with namespace for namespace-specific database.

    Returns the complete service endpoint including the infrastructure namespace
    to ensure proper DNS resolution across namespaces.

    Args:
        cluster_name: Name of the cluster
        project_name: Name of the project

    Returns:
        Full database service endpoint

    Examples:
        get_database_cluster_service_endpoint("local", "myproject")
        -> "myproject-db-rw.rig-myproject-infrastructure.svc.cluster.local"

        get_database_cluster_service_endpoint("odcn-production", "myproject")
        -> "myproject-db-rw.rig-prd-myproject-infrastructure.svc.cluster.local"
    """
    from opi.utils.naming import _sanitize_for_lowercase

    infrastructure_namespace = get_infrastructure_namespace(cluster_name, project_name)
    project_clean = _sanitize_for_lowercase(project_name)
    return f"{project_clean}-db-rw.{infrastructure_namespace}.svc.cluster.local"
