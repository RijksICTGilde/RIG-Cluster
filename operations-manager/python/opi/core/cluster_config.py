"""
Cluster configuration for different environments.

This module defines cluster-specific settings including ingress postfixes.
"""

import subprocess
from pathlib import Path

# TODO: In the future, read this configuration from YAML file
CLUSTER_CONFIG = {
    "local": {
        "ingress_postfix": ".kind",
        "namespace_prefix": "rig-",
        "argo_namespace": "rig-system",
        "namespace": "rig-system",
        "keycloak_discovery_url": "https://keycloak.kind",  # For pods in cluster
        "database_server": "rig-db-rw.rig-system.svc.cluster.local",
        "minio_host": "minio.rig-system.svc.cluster.local",
        "minio_port": 9000,
        "redis_server": "rig-redis.rig-system.svc.cluster.local",
        "ingress": {
            "enable_tls": True,
            "cluster_issuer": "kind-ca-issuer",
            "ip_whitelist": "0.0.0.0",
        },
        "storage": {"storage_class_name": "standard", "access_modes": ["ReadWriteOnce"]},
        "keycloak": {
            "support_http": True,  # Generate both HTTP and HTTPS redirect URIs
        },
        "uses_capsule": False,
        "ca_certificate": {
            "enabled": True,
            "node_path": "/etc/ssl/certs/kind-local-ca.crt",
            "container_path": "/etc/ssl/certs/custom-ca.crt",
            "env_vars": {
                "REQUESTS_CA_BUNDLE": "/etc/ssl/certs",
                "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/custom-ca.crt",
            },
        },
        "letsencrypt": {
            "contact_email": "rig-platform@rijksoverheid.nl",  # Default contact for Let's Encrypt certificates
        },
    },
    "odcn-production": {
        "ingress_postfix": ".rig.prd1.gn2.quattro.rijksapps.nl",
        "namespace_prefix": "rig-prd-",
        "namespace": "rig-prd-operations",
        "argo_namespace": "rig-prd-operations",
        "keycloak_discovery_url": "https://keycloak.rig.prd1.gn2.quattro.rijksapps.nl",  # For pods in cluster
        "database_server": "rig-db-rw.rig-prd-operations.svc.cluster.local",  # Assuming production DB is in operations namespace
        "minio_host": "minio.rig-prd-operations.svc.cluster.local",
        "minio_port": 9000,
        "redis_server": "rig-redis.rig-prd-operations.svc.cluster.local",
        "ingress": {
            "enable_tls": True,
            # "cluster_issuer": "letsencrypt-production",  # TODO: verify correct issuer name
            "ip_whitelist": "0.0.0.0/0",  # VPN only: "147.181.0.0/16"
        },
        "storage": {"storage_class_name": "ocs-storagecluster-ceph-rbd", "access_modes": ["ReadWriteOnce"]},
        "keycloak": {
            "support_http": False,  # Only generate HTTPS redirect URIs in production
        },
        "uses_capsule": True,
        "letsencrypt": {
            "contact_email": "rig-platform@rijksoverheid.nl",  # Default contact for Let's Encrypt certificates
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


def get_ingress_cluster_issuer(cluster_name: str) -> str | None:
    """
    Get the cert-manager ClusterIssuer name for TLS certificates.

    Args:
        cluster_name: Name of the cluster

    Returns:
        ClusterIssuer name string, or None if not configured

    Raises:
        ValueError: If cluster is not found in configuration
    """
    ingress_config = get_ingress_config(cluster_name)
    return ingress_config.get("cluster_issuer")


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


def get_minio_host(cluster_name: str) -> str:
    """
    Get the minio server hostname for pods in a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        minio server hostname string for internal pod use

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["minio_host"]


def get_minio_port(cluster_name: str) -> int:
    """
    Get the minio server port for pods in a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        minio server port integer for internal pod use

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["minio_port"]


def get_minio_server(cluster_name: str) -> str:
    """
    Get the minio server address (host:port) for pods in a specific cluster.

    This is the hostname:port that pods will use to connect to the minio internally.
    For separate host and port values, use get_minio_host() and get_minio_port().

    Args:
        cluster_name: Name of the cluster

    Returns:
        minio server address string (host:port) for internal pod use

    Raises:
        ValueError: If cluster is not found in configuration
    """
    return f"{get_minio_host(cluster_name)}:{get_minio_port(cluster_name)}"


def get_namespace(cluster_name: str) -> str:
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["namespace"]


def get_redis_server(cluster_name: str) -> str:
    """
    Get the Redis server hostname for pods in a specific cluster.

    This is the hostname that pods will use to connect to Redis internally.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Redis server hostname string for internal pod use

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["redis_server"]


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


def uses_capsule(cluster_name: str) -> bool:
    """
    Check if the cluster uses Capsule multi-tenancy.

    When Capsule is enabled, namespace creation requires waiting for
    Capsule to assign the tenant label before modifications can be made.

    Args:
        cluster_name: Name of the cluster

    Returns:
        True if the cluster uses Capsule, False otherwise

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("uses_capsule", False)


def get_letsencrypt_contact_email(cluster_name: str) -> str | None:
    """
    Get the default Let's Encrypt contact email for a specific cluster.

    This email is used for ACME account registration and certificate expiry notifications.
    Projects can override this with their own contact-email in the project config.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Contact email string, or None if not configured

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    letsencrypt_config = cluster_config.get("letsencrypt", {})
    return letsencrypt_config.get("contact_email")


def _compute_ca_hash(cert_path: str) -> str | None:
    """
    Compute the OpenSSL hash of a CA certificate.

    This hash is used by OpenSSL for directory-based CA lookup.
    The hash is computed using: openssl x509 -hash -noout -in <cert>

    Args:
        cert_path: Path to the CA certificate file

    Returns:
        The 8-character hash string, or None if computation fails
    """
    if not Path(cert_path).exists():
        return None

    try:
        result = subprocess.run(
            ["openssl", "x509", "-hash", "-noout", "-in", cert_path],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_ca_certificate_config(cluster_name: str) -> dict | None:
    """
    Get the CA certificate configuration for a specific cluster.

    This function returns the CA certificate configuration enriched with
    the computed OpenSSL hash for directory-based CA lookup.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Dictionary containing CA certificate configuration with keys:
        - enabled: Whether CA certificate injection is enabled
        - node_path: Path to the CA cert on the Kubernetes node
        - container_path: Path where the CA cert is mounted in containers
        - hash: OpenSSL hash for directory-based lookup (computed at runtime)
        - env_vars: Environment variables to set for SSL libraries

        Returns None if CA certificate is not configured for this cluster.

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    ca_config = cluster_config.get("ca_certificate")

    if ca_config is None or not ca_config.get("enabled", False):
        return None

    node_path = ca_config["node_path"]
    ca_hash = _compute_ca_hash(node_path)

    return {
        "enabled": True,
        "node_path": node_path,
        "container_path": ca_config["container_path"],
        "hash": ca_hash,
        "env_vars": ca_config.get("env_vars", {}),
    }
