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
