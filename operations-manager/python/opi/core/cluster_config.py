"""
Cluster configuration for different environments.

This module defines cluster-specific settings including ingress postfixes.
"""

import subprocess
from pathlib import Path
from typing import Any

from opi.core.config import settings
from opi.utils.naming import generate_keycloak_sender_address

# TODO: In the future, read this configuration from YAML file
CLUSTER_CONFIG = {
    "local": {
        # Development offers a choice of target clusters in the create wizard; production
        # (odcn-production) omits this key and so offers only itself. See
        # get_selectable_clusters().
        "create_wizard_clusters": ["local", "sandboxed-local", "odcn-production"],
        "ingress_postfix": ".kind",
        "namespace_prefix": "rig-",
        "argo_namespace": "rig-system",
        "namespace": "rig-system",
        "keycloak_discovery_url": "https://keycloak.kind",  # For pods in cluster
        "database_server": "rig-db-rw.rig-system.svc.cluster.local",
        "minio_host": "minio.rig-system.svc.cluster.local",
        "minio_port": 9000,
        "redis_server": "rig-redis.rig-system.svc.cluster.local",
        "backup_namespace": "rig-backup-destination",
        "mail_relay_namespace": "rig-ron",
        "mail_relay_host": "rig-mail-relay.rig-ron.svc.cluster.local",
        "mail_relay_port": 587,
        "mail_from_address": "noreply-rijksapp@rijksoverheid.nl",
        # Namespace of the CloudNativePG operator, which must reach the dedicated
        # CNPG cluster's pods to extract instance status; the infra-namespace
        # NetworkPolicy allows ingress from here.
        "database_operator_namespace": "cnpg-system",
        "ingress_controller_selector": {
            "namespace": "ingress-nginx",
            "pod_labels": {},
        },
        "ingress": {
            "enable_tls": True,
            "cluster_issuer": "kind-ca-issuer",
            "ip_whitelist": "0.0.0.0",
        },
        "storage": {
            "storage_class_name": "csi-hostpath-sc",
            "access_modes": ["ReadWriteOnce"],
            "volume_snapshot_class": "csi-hostpath-snapclass",
        },
        "keycloak": {
            "support_http": True,  # Generate both HTTP and HTTPS redirect URIs
        },
        "min_memory_limit_mi": 25,
        "max_memory_limit_mi": 4096,
        "max_memory_request_mi": 1024,
        "uses_capsule": False,
        "min_cpu_m": 25,
        "max_cpu_request_m": 250,
        "max_cpu_limit_m": 4000,
        "supports_vpa": False,
        # Geen supports_custom_domain_certificates hier: dit cluster draait een eigen CA
        # (cluster_issuer kind-ca-issuer) en of die ook een eigen domein tekent is niet
        # nagemeten. Afwezig betekent zwijgen, en dat is het eerlijke antwoord bij een
        # cluster waarvan we het niet weten.
        "ca_certificate": {
            "enabled": True,
            "node_path": "/etc/ssl/certs/kind-local-ca.crt",
            "container_path": "/etc/ssl/certs/custom-ca.crt",
            "env_vars": {
                "REQUESTS_CA_BUNDLE": "/etc/ssl/certs",
                "NODE_EXTRA_CA_CERTS": "/etc/ssl/certs/custom-ca.crt",
                "SSL_CERT_DIR": "/etc/ssl/certs",  # For Python's native SSL (httpx, etc.)
            },
        },
        "letsencrypt": {
            "contact_email": "rig-platform@rijksoverheid.nl",  # Default contact for Let's Encrypt certificates
        },
        "nice_url": {
            "supported_domains": [
                {"domain": "kind", "supports_dots": True, "restricted_subdomains": True},
                {"domain": "local", "supports_dots": True, "restricted_subdomains": True},
            ],
        },
    },
    "sandboxed-local": {
        "ingress_postfix": ".sandbox.rijksapp.dev",
        "namespace_prefix": "rig-",
        "argo_namespace": "rig-system",
        "namespace": "rig-system",
        "keycloak_discovery_url": "https://keycloak.sandbox.rijksapp.dev",
        "database_server": "rig-db-rw.rig-system.svc.cluster.local",
        "minio_host": "minio.rig-system.svc.cluster.local",
        "minio_port": 9000,
        "redis_server": "rig-redis.rig-system.svc.cluster.local",
        "backup_namespace": "rig-backup-destination",
        "mail_relay_namespace": "rig-ron",
        "mail_relay_host": "rig-mail-relay.rig-ron.svc.cluster.local",
        "mail_relay_port": 587,
        "mail_from_address": "noreply-rijksapp@rijksoverheid.nl",
        # Namespace of the CloudNativePG operator, which must reach the dedicated
        # CNPG cluster's pods to extract instance status; the infra-namespace
        # NetworkPolicy allows ingress from here.
        "database_operator_namespace": "cnpg-system",
        "ingress_controller_selector": {
            "namespace": "ingress-nginx",
            "pod_labels": {},
        },
        "ingress": {
            "enable_tls": True,
            "ip_whitelist": "0.0.0.0/0,::/0",
        },
        "storage": {
            "storage_class_name": "csi-hostpath-sc",
            "access_modes": ["ReadWriteOnce"],
            "volume_snapshot_class": "csi-hostpath-snapclass",
        },
        "keycloak": {
            "support_http": False,
        },
        "min_memory_limit_mi": 25,
        "max_memory_limit_mi": 4096,
        "max_memory_request_mi": 1024,
        "uses_capsule": False,
        "min_cpu_m": 25,
        "max_cpu_request_m": 250,
        "max_cpu_limit_m": 4000,
        "supports_vpa": False,
        # The sandbox serves *.sandbox.rijksapp.dev from a pre-installed wildcard
        # certificate and runs a fake cert-manager CRD with no controller, so nothing is
        # ever issued here. See supports_custom_domain_certificates().
        "supports_custom_domain_certificates": False,
        # Er is geen VLAM en geen RON in de sandbox: dit is een PLAATSHOUDER, alleen
        # zodat de bedrading van de dienst (kaart, env-var, netwerkregel) hier
        # end-to-end te doorlopen is. Het adres wijst naar een project dat hier niet
        # bestaat, dus een pod die het probeert krijgt geen antwoord. Zie
        # features/vlam-service.md.
        "vlam": {
            "project": "vlam-wt8",
            "deployment": "productie",
            "component": "vlam-proxy-intern",
            "namespace": "vlam-wt8",
            "port": 8081,
        },
        "letsencrypt": {
            "contact_email": "rig-platform@rijksoverheid.nl",
        },
        "nice_url": {
            "supported_domains": [
                {"domain": "sandbox.rijksapp.dev", "supports_dots": False, "restricted_subdomains": True},
                {
                    "domain": "robbertuittenbroek.nl",
                    "supports_dots": True,
                    "issuer": "letsencrypt",
                    "restricted_subdomains": True,
                },
            ],
        },
    },
    "odcn-production": {
        "ingress_postfix": ".rig.prd1.gn2.quattro.rijksapps.nl",
        "namespace_prefix": "rig-prd-",
        "namespace": "rig-prd-operations",
        "argo_namespace": "rig-prd-operations",
        "keycloak_discovery_url": "https://keycloak.rijksapp.nl",
        "database_server": "rig-db-rw.rig-prd-operations.svc.cluster.local",  # Assuming production DB is in operations namespace
        "minio_host": "minio.rig-prd-operations.svc.cluster.local",
        "minio_port": 9000,
        "redis_server": "rig-redis.rig-prd-operations.svc.cluster.local",
        "backup_namespace": "rig-prd-backup",
        # ODCN eist dat een namespace op dat cluster met de clusterprefix begint, dus daar
        # heet hij rig-prd-ron; op local en sandbox rig-ron. Zelfde vorm als
        # backup_namespace hierboven.
        "mail_relay_namespace": "rig-prd-ron",
        "mail_relay_host": "rig-mail-relay.rig-prd-ron.svc.cluster.local",
        "mail_relay_port": 587,
        "mail_from_address": "noreply-rijksapp@rijksoverheid.nl",
        # Namespace of the CloudNativePG operator (see the note in the other clusters).
        "database_operator_namespace": "cnpg-system",
        "ingress_controller_selector": {
            "namespace": "openshift-ingress",
            "pod_labels": {
                "ingresscontroller.operator.openshift.io/deployment-ingresscontroller": "rig",
            },
        },
        "ingress": {
            "enable_tls": True,
            # "cluster_issuer": "letsencrypt-production",  # TODO: verify correct issuer name
            "ip_whitelist": "0.0.0.0/0,::/0",  # VPN only: "147.181.0.0/16"
        },
        "storage": {
            "storage_class_name": "ocs-storagecluster-ceph-rbd",
            "access_modes": ["ReadWriteOnce"],
            "volume_snapshot_class": "ocs-storagecluster-rbdplugin-snapclass",
        },
        "keycloak": {
            "support_http": False,  # Only generate HTTPS redirect URIs in production
        },
        "min_memory_limit_mi": 25,
        "max_memory_limit_mi": 4096,
        "max_memory_request_mi": 1024,
        "uses_capsule": True,
        "min_cpu_m": 25,
        "max_cpu_request_m": 250,
        "max_cpu_limit_m": 4000,
        "supports_vpa": True,
        # Reachable from the internet and running a real cert-manager, so an ACME HTTP-01
        # challenge for a domain of the user's own can complete here.
        "supports_custom_domain_certificates": True,
        # VLAM (de taalmodel-API van SSC-ICT) is alleen hier bereikbaar: de RON-koppeling
        # bestaat op dit cluster en nergens anders. De sleutels beschrijven WAAR de
        # interne proxy van het vlam-project draait; de dienst leidt daar zowel het
        # adres als de netwerkregel uit af, zodat die twee niet uiteen kunnen lopen.
        # ``namespace`` is de onvoorvoegde naam uit het projectbestand; het cluster zet
        # er ``rig-prd-`` voor (get_prefixed_namespace).
        "vlam": {
            "project": "vlam-wt8",
            "deployment": "productie",
            "component": "vlam-proxy-intern",
            "namespace": "vlam-wt8",
            "port": 8081,
        },
        "letsencrypt": {
            "contact_email": "rig-platform@rijksoverheid.nl",  # Default contact for Let's Encrypt certificates
        },
        "nice_url": {
            "supported_domains": [
                {
                    "domain": "rijks.app",
                    "supports_dots": True,
                    "issuer": "letsencrypt",
                    "restricted_subdomains": True,
                    "external_dns_target": "router.rijks.app",
                },
                {
                    "domain": "rijksapp.nl",
                    "supports_dots": True,
                    "issuer": "letsencrypt",
                    "restricted_subdomains": True,
                    "external_dns_target": "router.rijksapp.nl",
                },
                {
                    "domain": "rijksapp.dev",
                    "supports_dots": True,
                    "issuer": "letsencrypt",
                    "restricted_subdomains": True,
                    "external_dns_target": "router.rijksapp.dev",
                },
            ],
        },
        "extensions": ["odcn-registry-rewrite"],
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


def get_selectable_clusters() -> list[str]:
    """Clusters offered as a target in the create-project wizard.

    Driven by the managing cluster's own config key ``create_wizard_clusters``, and it
    defaults to just the managing cluster. So production (CLUSTER_MANAGER =
    odcn-production) offers only odcn-production and can never list a dev cluster by
    accident, while a development overlay sets the key to offer several. Unknown names
    are dropped, so a typo in the config cannot surface a non-existent cluster.
    """
    from opi.core.config import settings

    manager = settings.CLUSTER_MANAGER
    configured = get_cluster_config(manager).get("create_wizard_clusters", [manager])
    return [c for c in configured if c in CLUSTER_CONFIG]


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


def get_volume_snapshot_class(cluster_name: str) -> str | None:
    """
    Get the VolumeSnapshotClass name for a specific cluster.

    This is used when creating VolumeSnapshots for PVC backups.

    Args:
        cluster_name: Name of the cluster

    Returns:
        VolumeSnapshotClass name string, or None if not configured

    Raises:
        ValueError: If cluster is not found in configuration
    """
    storage_config = get_storage_config(cluster_name)
    return storage_config.get("volume_snapshot_class")


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
    return cluster_config.get("ingress", {"enable_tls": False, "ip_whitelist": "0.0.0.0/0,::/0"})


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


def get_backup_namespace(cluster_name: str) -> str:
    """
    Get the namespace that hosts the backup destination for a cluster.

    This is where the cluster's backup MinIO (or comparable) lives, so
    workloads can reach it for restores/snapshots. NetworkPolicies use this
    as an allowed peer namespace.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Backup destination namespace name
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["backup_namespace"]


def get_database_operator_namespace(cluster_name: str) -> str:
    """Namespace of the CloudNativePG operator for a cluster.

    A dedicated (project-scoped) PostgreSQL cluster lives in the project's
    infrastructure namespace, but the CNPG operator that manages it runs in its own
    namespace and must reach the cluster's pods to extract their instance status.
    Without an ingress allowance for this namespace the operator reports
    "Instance Status Extraction Error", the Cluster never becomes Ready and its ArgoCD
    health stays Unknown. The infra-namespace NetworkPolicy uses this as an allowed peer.

    Defaults to ``cnpg-system`` (the CloudNativePG default install namespace) for
    clusters that predate this setting.

    Args:
        cluster_name: Name of the cluster

    Returns:
        CloudNativePG operator namespace name
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("database_operator_namespace", "cnpg-system")


def get_ingress_controller_selector(cluster_name: str) -> dict:
    """
    Return de selector voor de ingress-controller pods van een cluster.

    Format::

        {"namespace": "<ns>", "pod_labels": {<key>: <val>, ...}}

    pod_labels is leeg voor nginx-clusters; voor odcn (OpenShift Router) bevat
    het de ``ingresscontroller.operator.openshift.io/deployment-ingresscontroller``
    label zodat NetworkPolicy alleen de juiste customer-router toelaat.
    Zie docs/knowledge/odcn-ingress-controller.md.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["ingress_controller_selector"]


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


def get_mail_relay_namespace(cluster_name: str) -> str:
    """
    Get the namespace the SMTP relay runs in.

    Its own namespace and not the operations namespace: the Calico annotation
    ``egress.projectcalico.org/egressGatewayPolicy`` takes exactly ONE value, so a
    namespace can have RON egress or internet egress, never both. The operations
    namespace needs internet (ArgoCD, the registry, Keycloak), so the relay lives
    apart. See ``plans/mailrelay.md``.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Namespace name the relay and its Service live in

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["mail_relay_namespace"]


def get_mail_relay_host(cluster_name: str) -> str:
    """
    Get the in-cluster hostname of the SMTP relay.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Relay hostname for internal pod use

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["mail_relay_host"]


def get_mail_relay_port(cluster_name: str) -> int:
    """
    Get the submission port of the SMTP relay.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Submission port (587) for internal pod use

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["mail_relay_port"]


def get_mail_from_address(cluster_name: str) -> str:
    """
    The BASE sender address of this cluster: the bare one, without a plus part.

    Not what a project sends from -- that is ``<local>+<project>@<domain>``, composed by
    ``generate_mail_sender_address`` and handed to the relay by ``MailManager``. What lives
    here is the pair the relay itself is configured with (MAIL_FROM_LOCAL and MAIL_DOMAIN
    in its secret), so composing happens in ONE place instead of here as well.

    It is also the FALLBACK: a message from an account the relay holds no sender for goes
    out under exactly this address, without a display name. So is the mail from ZAD's own
    platform account, which is not a project and has no plus part to fill.

    OPI and the relay must agree on this value -- if they drift, a developer is shown one
    address while another one leaves the building.

    It is a domain we do NOT own: mail goes out over the Rijksoverheid mail server, so it
    carries their domain. That is also the only arrangement that survives DMARC, because
    they publish ``p=reject`` and we sign nothing with DKIM, leaving SPF alignment between
    envelope and ``From:`` as the single thing that can pass. See docs/ron-koppeling.md.

    Args:
        cluster_name: Name of the cluster

    Returns:
        The bare sender address (e.g. ``noreply-rijksapp@rijksoverheid.nl``)

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["mail_from_address"]


def get_keycloak_mail_from_address(cluster_name: str) -> str:
    """The address KEYCLOAK's login mail leaves under on this cluster.

    Its own local part next to the portal's, on the cluster's own domain. ONE derivation
    for the whole platform and not two, because this address is written down in three
    places that have to agree: the relay gets it as this account's sender (MailManager),
    the Keycloak pod gets it as ``ZAD_MAIL_RELAY_FROM``, and OPI writes it into every
    realm's minimal ``smtpServer``. Drift between them shows up as a message that leaves
    under one address while a realm claims another.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Keycloak's sender address (e.g. ``noreply-inloggen@rijksoverheid.nl``)

    Raises:
        ValueError: If cluster is not found in configuration
    """
    return generate_keycloak_sender_address(get_mail_from_address(cluster_name), settings.MAIL_KEYCLOAK_FROM_LOCAL)


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


def get_min_memory_limit_mi(cluster_name: str) -> int:
    """
    Get the minimum memory limit in Mi for a specific cluster.

    This prevents setting memory limits below what the container runtime accepts.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Minimum memory limit in Mi
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("min_memory_limit_mi", 25)


def get_max_memory_limit_mi(cluster_name: str) -> int:
    """
    Get the maximum memory limit in Mi for a specific cluster.

    This is the upper bound for auto-tuning and wizard dropdowns.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Maximum memory limit in Mi
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("max_memory_limit_mi", 4096)


def get_max_memory_request_mi(cluster_name: str) -> int:
    """
    Get the maximum memory request in Mi for a specific cluster.

    Memory requests are capped lower than limits. Below this cap, requests
    and limits scale together. Above it, only limits can increase further.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Maximum memory request in Mi
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("max_memory_request_mi", 1024)


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


def supports_vpa(cluster_name: str) -> bool:
    """
    Check if the cluster provides a Vertical Pod Autoscaler recommender.

    When True, the platform runs a VPA recommender that publishes resource
    recommendations to VerticalPodAutoscaler objects. Auto-tuning then sources
    its recommendations from VPA (memory + CPU) instead of Prometheus.

    Args:
        cluster_name: Name of the cluster

    Returns:
        True if the cluster can create VPA objects and run a recommender
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("supports_vpa", False)


def supports_custom_domain_certificates(cluster_name: str) -> bool:
    """Whether this cluster can obtain a certificate for a domain of the user's own.

    A domain outside the cluster's ``nice_url.supported_domains`` gets no certificate for
    free: the platform certificate covers the supported domains only, so cert-manager has
    to issue one, over an ACME HTTP-01 challenge that the outside world must be able to
    reach. On production that works. On the two Kind clusters it cannot: they are not
    reachable from the internet, and ``task sandbox:setup`` even installs a FAKE
    cert-manager CRD (``bootstrap/crd/cert-manager/fake-cert-manager.yaml``) with no
    controller behind it, so the Issuer applies, reports Ready, and nothing is ever
    issued. Everything stays green and the site serves the wrong certificate -- which is
    exactly how this was discovered (zad-cli, bevinding 22).

    So this is a capability of the cluster and not a property of the domain, and it says
    only what the platform will do, never whether the DNS or the ownership is in order.

    Absent means True: silence is the right answer for a cluster that has not declared
    this, since a warning nobody configured would be a guess about someone else's cluster.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("supports_custom_domain_certificates", True)


def get_min_cpu_m(cluster_name: str) -> int:
    """
    Get the minimum CPU value in millicores for a specific cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Minimum CPU in millicores
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("min_cpu_m", 25)


def get_max_cpu_request_m(cluster_name: str) -> int:
    """
    Get the maximum CPU request in millicores for auto-tuning.

    Auto-tuning never sets a CPU request above this ceiling; a pod that
    genuinely needs more requires manual intervention.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Maximum CPU request in millicores
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("max_cpu_request_m", 250)


def get_max_cpu_limit_m(cluster_name: str) -> int:
    """
    Get the maximum CPU limit in millicores for auto-tuning.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Maximum CPU limit in millicores
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("max_cpu_limit_m", 4000)


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
    except subprocess.CalledProcessError, FileNotFoundError:
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


def get_nice_url_config(cluster_name: str) -> dict | None:
    """
    Get the nice URL configuration for a specific cluster.

    Nice URLs use dot-separated patterns like component.deployment.base_domain
    instead of the default dash-separated patterns.

    Args:
        cluster_name: Name of the cluster

    Returns:
        Dictionary containing nice URL configuration with keys:
        - supported_domains: List of domains that support the nice URL pattern

        Returns None if nice URLs are not configured for this cluster.

    Raises:
        ValueError: If cluster is not found in configuration
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("nice_url")


def get_nice_url_supported_domains(cluster_name: str) -> list[str]:
    """
    Get the list of domains that support nice URLs for a specific cluster.

    Extracts domain strings from the structured supported_domains list
    for backward compatibility.

    Args:
        cluster_name: Name of the cluster

    Returns:
        List of domain strings that support nice URL pattern.
        Returns empty list if nice URLs are not configured.

    Raises:
        ValueError: If cluster is not found in configuration
    """
    nice_url_config = get_nice_url_config(cluster_name)
    if nice_url_config is None:
        return []
    raw = nice_url_config.get("supported_domains", [])
    return [entry["domain"] if isinstance(entry, dict) else entry for entry in raw]


def is_nice_url_domain_supported(cluster_name: str, base_domain: str) -> bool:
    """
    Check if a specific base domain supports nice URLs on a cluster.

    Args:
        cluster_name: Name of the cluster
        base_domain: The base domain to check (e.g., "rijks.app")

    Returns:
        True if the domain supports nice URLs on this cluster, False otherwise.

    Raises:
        ValueError: If cluster is not found in configuration
    """
    supported_domains = get_nice_url_supported_domains(cluster_name)
    return base_domain in supported_domains


def get_domain_issuer(cluster_name: str, domain: str) -> str | None:
    """
    Get the issuer for a specific domain on a cluster.

    Looks up the domain in the cluster's supported_domains list.
    Returns the per-domain issuer if configured, otherwise falls back
    to the cluster's default cluster_issuer.

    Args:
        cluster_name: Name of the cluster
        domain: The domain to check (e.g., "rijksapp.nl")

    Returns:
        Issuer string (e.g., "letsencrypt") or None if no issuer is needed.

    Raises:
        ValueError: If cluster is not found in configuration
    """
    nice_url_config = get_nice_url_config(cluster_name)
    if nice_url_config is not None:
        for entry in nice_url_config.get("supported_domains", []):
            if isinstance(entry, dict) and entry.get("domain") == domain:
                return entry.get("issuer")
    return None


def get_external_dns_target_for_hostname(cluster_name: str, hostname: str) -> str | None:
    """
    Get the external-dns target for a hostname, based on the cluster's configured base domains.

    Walks the cluster's supported_domains and returns the configured external_dns_target
    of the most specific domain that the hostname falls under. Returns None when no
    configured domain matches (e.g. for hostnames in the cluster's default postfix zone,
    which gets its DNS via the OpenShift router and does not need an explicit target).

    Args:
        cluster_name: Name of the cluster
        hostname: The hostname to resolve (e.g., "myproject.rijksapp.nl")

    Returns:
        Target hostname for the external-dns annotation, or None if none configured.
    """
    nice_url_config = get_nice_url_config(cluster_name)
    if nice_url_config is None:
        return None

    candidates = [
        entry
        for entry in nice_url_config.get("supported_domains", [])
        if isinstance(entry, dict) and entry.get("external_dns_target")
    ]
    # Sort longest domain first so more specific bases match before less specific ones.
    candidates.sort(key=lambda e: -len(e["domain"]))

    for entry in candidates:
        domain = entry["domain"]
        if hostname == domain or hostname.endswith("." + domain):
            return entry["external_dns_target"]
    return None


def is_domain_subdomain_restricted(cluster_name: str, domain: str) -> bool:
    """
    Check if a specific domain has restricted subdomains on a cluster.

    When restricted, only subdomains explicitly listed in the project's
    allowed-subdomains section may be used.

    Args:
        cluster_name: Name of the cluster
        domain: The domain to check (e.g., "rijks.app")

    Returns:
        True if the domain restricts subdomains, False otherwise.

    Raises:
        ValueError: If cluster is not found in configuration
    """
    nice_url_config = get_nice_url_config(cluster_name)
    if nice_url_config is None:
        return False
    for entry in nice_url_config.get("supported_domains", []):
        if isinstance(entry, dict) and entry.get("domain") == domain:
            return entry.get("restricted_subdomains", False)
    return False


def get_restricted_subdomain_domains(cluster_name: str) -> list[str]:
    """
    Get domains that have subdomain restrictions enabled on a cluster.

    Args:
        cluster_name: Name of the cluster

    Returns:
        List of domain strings with restricted_subdomains=True.

    Raises:
        ValueError: If cluster is not found in configuration
    """
    nice_url_config = get_nice_url_config(cluster_name)
    if nice_url_config is None:
        return []
    return [
        entry["domain"]
        for entry in nice_url_config.get("supported_domains", [])
        if isinstance(entry, dict) and entry.get("restricted_subdomains", False)
    ]


def get_domain_supports_dots(cluster_name: str, domain: str) -> bool:
    """
    Check if a specific domain supports dot-separated hostnames on a cluster.

    Args:
        cluster_name: Name of the cluster
        domain: The domain to check (e.g., "rijks.app")

    Returns:
        True if the domain supports dot-separated hostnames, False otherwise.

    Raises:
        ValueError: If cluster is not found in configuration
    """
    nice_url_config = get_nice_url_config(cluster_name)
    if nice_url_config is None:
        return False
    for entry in nice_url_config.get("supported_domains", []):
        if isinstance(entry, dict) and entry.get("domain") == domain:
            return entry.get("supports_dots", False)
    return False


def get_extensions(cluster_name: str) -> list[str]:
    """Get the list of manifest extension names configured for a cluster.

    Returns an empty list if no extensions are configured.
    """
    config = get_cluster_config(cluster_name)
    return config.get("extensions", [])


def get_vlam_config(cluster_name: str) -> dict[str, Any] | None:
    """Where the in-cluster VLAM proxy runs on this cluster, or None when there is none.

    Absent is the normal answer: VLAM hangs off the RON link, which exists on exactly one
    cluster. A cluster without this key neither offers the ``vlam`` service in the wizard
    nor accepts a project that selected it -- that is the whole availability mechanism,
    and it lives here so the service itself names no cluster.

    Keys: ``project`` / ``deployment`` / ``component`` / ``namespace`` (unprefixed) of the
    proxy, plus its ``port``. The address a consumer gets and the NetworkPolicy peer it is
    allowed to reach are BOTH derived from these, so they cannot drift apart.
    """
    return get_cluster_config(cluster_name).get("vlam")
