"""
Cluster configuration for different environments.

This module defines cluster-specific settings including ingress postfixes.
"""

import logging
import subprocess
from pathlib import Path
from typing import Any

from opi.core.config import settings
from opi.core.subprocess_env import child_env

logger = logging.getLogger(__name__)

#: The S3 API of MinIO; sits in minio_port as well as in the egress rule below.
MINIO_PORT = 9000

#: The S3 API of SeaweedFS; sits in seaweedfs_port as well as in the egress rule below.
SEAWEEDFS_S3_PORT = 8333

# Pod and service CIDRs of the ODCN cluster, for the except of the internet rule.
# Derived on 2026-10-21 because network.config/cluster is not readable.
ODCN_CLUSTER_CIDRS = {
    "ipv4": ["10.128.0.0/14", "172.30.0.0/16"],
    "ipv6": ["2a04:9a00:1007:4000::/64"],
}

# Shared services in the operations namespace: each on its own port.
OPS_EGRESS = [
    {"endpoint": "database_server", "pod_labels": {"cnpg.io/cluster": "rig-db"}, "ports": [5432]},
    {"endpoint": "redis_server", "pod_labels": {"app": "rig-redis"}, "ports": [6379]},
    {"endpoint": "minio_host", "pod_labels": {"app": "minio"}, "ports": [MINIO_PORT]},
    {"endpoint": "operations_manager", "pod_labels": {"app": "operations-manager"}, "ports": [8000]},
]

# The egress rule to SeaweedFS is not in OPS_EGRESS but follows from the cluster key
# seaweedfs_host: get_ops_egress() adds it for a cluster that offers the service. That keeps
# "this cluster offers seaweedfs" in one place, and creates no copy of the whole list per
# cluster.
SEAWEEDFS_OPS_EGRESS = {
    "endpoint": "seaweedfs_host",
    "pod_labels": {"app": "seaweedfs"},
    "ports": [SEAWEEDFS_S3_PORT],
}

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
        "minio_port": MINIO_PORT,
        "redis_server": "rig-redis.rig-system.svc.cluster.local",
        "backup_namespace": "rig-backup-destination",
        "mail_relay_namespace": "rig-ron",
        "mail_relay_host": "rig-mail-relay.rig-ron.svc.cluster.local",
        # The namespaces of the services WE run; see service_namespaces at odcn-production
        # for why this is a list and not a derivation.
        "service_namespaces": ["rig-system", "rig-backup-destination", "rig-ron"],
        "mail_relay_port": 587,
        "mail_from_address": "noreply-rijksapp@rijksoverheid.nl",
        # Namespace of the CloudNativePG operator, which must reach the dedicated
        # CNPG cluster's pods to extract instance status; the infra-namespace
        # NetworkPolicy allows ingress from here.
        "database_operator_namespace": "cnpg-system",
        "ops_egress": OPS_EGRESS,
        # Pod and service CIDR for the except of the internet rule; single-stack IPv4 only.
        "cluster_cidrs": {"ipv4": ["10.244.0.0/16", "10.96.0.0/16"]},
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
            "demo_users": True,  # Blueprint demo users with a password out of the repo are allowed here
        },
        "min_memory_limit_mi": 25,
        "max_memory_limit_mi": 4096,
        "max_memory_request_mi": 1024,
        "uses_capsule": False,
        # Pod Security Admission per namespace; see features/pod-security-labels.md.
        # NOT enforced here: ca_certificate.enabled mounts the node's CA with a hostPath,
        # and baseline forbids that. Warning does give the list.
        "pod_security": {"enforce": "privileged", "warn": "baseline", "audit": "baseline"},
        "min_cpu_m": 25,
        "max_cpu_request_m": 250,
        "max_cpu_limit_m": 4000,
        "supports_vpa": False,
        "oom_pod_watch": False,
        # No supports_custom_domain_certificates here: this cluster runs a CA of its own
        # (cluster_issuer kind-ca-issuer) and whether that one signs an own domain too has not
        # been measured. Absent means silence, and that is the honest answer for a cluster we
        # do not know it for.
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
        "domains": {
            "supported_domains": [
                {"domain": "kind", "supports_dots": True, "restricted_subdomains": True},
                {"domain": "local", "supports_dots": True, "restricted_subdomains": True},
            ],
            # The zones ZAD serves itself. A fact of its own next to supported_domains: that
            # is an OFFER list, and a zone can be ours without being offered.
            "managed_zones": ["kind", "local"],
        },
        # The nodes can reach the registry themselves: a dockerconfigjson secret in the
        # namespace, image unchanged. No "rules", because there is no proxy table.
        "image_registries": {"backend": "direct-secret"},
    },
    "sandboxed-local": {
        "ingress_postfix": ".sandbox.rijksapp.dev",
        "namespace_prefix": "rig-",
        "argo_namespace": "rig-system",
        "namespace": "rig-system",
        "keycloak_discovery_url": "https://keycloak.sandbox.rijksapp.dev",
        "database_server": "rig-db-rw.rig-system.svc.cluster.local",
        "minio_host": "minio.rig-system.svc.cluster.local",
        "minio_port": MINIO_PORT,
        # The switch of seaweedfs-storage (RC-331): only a cluster with these two keys offers
        # the service, accepts a project that names it, and gets the egress rule to 8333 in
        # its tenant baseline.
        "seaweedfs_host": "seaweedfs.rig-system.svc.cluster.local",
        "seaweedfs_port": SEAWEEDFS_S3_PORT,
        "redis_server": "rig-redis.rig-system.svc.cluster.local",
        "backup_namespace": "rig-backup-destination",
        "mail_relay_namespace": "rig-ron",
        "mail_relay_host": "rig-mail-relay.rig-ron.svc.cluster.local",
        # The namespaces of the services WE run; see service_namespaces at odcn-production
        # for why this is a list and not a derivation.
        "service_namespaces": ["rig-system", "rig-backup-destination", "rig-ron"],
        "mail_relay_port": 587,
        "mail_from_address": "noreply-rijksapp@rijksoverheid.nl",
        # Namespace of the CloudNativePG operator, which must reach the dedicated
        # CNPG cluster's pods to extract instance status; the infra-namespace
        # NetworkPolicy allows ingress from here.
        "database_operator_namespace": "cnpg-system",
        "ops_egress": OPS_EGRESS,
        # Pod and service CIDR for the except of the internet rule; single-stack IPv4 only.
        "cluster_cidrs": {"ipv4": ["10.244.0.0/16", "10.96.0.0/16"]},
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
            "demo_users": True,
        },
        "min_memory_limit_mi": 25,
        "max_memory_limit_mi": 4096,
        "max_memory_request_mi": 1024,
        "uses_capsule": False,
        # Pod Security Admission per namespace; see features/pod-security-labels.md.
        "pod_security": {"enforce": "baseline", "warn": "restricted", "audit": "restricted"},
        "min_cpu_m": 25,
        "max_cpu_request_m": 250,
        "max_cpu_limit_m": 4000,
        "supports_vpa": False,
        "oom_pod_watch": False,
        # The sandbox serves *.sandbox.rijksapp.dev from a pre-installed wildcard
        # certificate and runs a fake cert-manager CRD with no controller, so nothing is
        # ever issued here. See supports_custom_domain_certificates().
        "supports_custom_domain_certificates": False,
        # There is no VLAM and no RON in the sandbox: this is a PLACEHOLDER, only so the
        # wiring of the service (card, env var, network rule) can be walked end to end here.
        # The address points at a project that does not exist here, so a pod that tries it
        # gets no answer. See features/vlam-service.md.
        "vlam": {
            "project": "vlam-wt8",
            "deployment": "productie",
            "component": "vlam-proxy-intern",
            "namespace": "vlam-wt8",
            "port": 8081,
            # The passthrough path is just as much a PLACEHOLDER as the rest of this block:
            # the stub listens on 8081 only and speaks no TLS, and the address below is not
            # pinned to the Service the stub creates. It is here so the block has the same
            # shape as the one of production.
            "passthrough_port": 8443,
            "api_host": "vlam-api.overheid-i.nl",
            "cluster_ip": "10.96.144.8",
        },
        "letsencrypt": {
            "contact_email": "rig-platform@rijksoverheid.nl",
        },
        "domains": {
            "supported_domains": [
                {"domain": "sandbox.rijksapp.dev", "supports_dots": False, "restricted_subdomains": True},
                {
                    "domain": "robbertuittenbroek.nl",
                    "supports_dots": True,
                    "issuer": "letsencrypt",
                    "restricted_subdomains": True,
                },
            ],
            "managed_zones": ["sandbox.rijksapp.dev", "robbertuittenbroek.nl"],
        },
        # The nodes can reach the registry themselves: a dockerconfigjson secret in the
        # namespace, image unchanged. No "rules", because there is no proxy table.
        "image_registries": {"backend": "direct-secret"},
    },
    "odcn-production": {
        "ingress_postfix": ".rig.prd1.gn2.quattro.rijksapps.nl",
        "namespace_prefix": "rig-prd-",
        "namespace": "rig-prd-operations",
        "argo_namespace": "rig-prd-operations",
        "keycloak_discovery_url": "https://keycloak.rijksapp.nl",
        "database_server": "rig-db-rw.rig-prd-operations.svc.cluster.local",  # Assuming production DB is in operations namespace
        "minio_host": "minio.rig-prd-operations.svc.cluster.local",
        "minio_port": MINIO_PORT,
        "seaweedfs_host": "seaweedfs.rig-prd-operations.svc.cluster.local",
        "seaweedfs_port": SEAWEEDFS_S3_PORT,
        "redis_server": "rig-redis.rig-prd-operations.svc.cluster.local",
        "backup_namespace": "rig-prd-backup",
        # ODCN requires a namespace on that cluster to start with the cluster prefix, so
        # there it is called rig-prd-ron; on local and sandbox rig-ron. The same shape as
        # backup_namespace above.
        "mail_relay_namespace": "rig-prd-ron",
        "mail_relay_host": "rig-mail-relay.rig-prd-ron.svc.cluster.local",
        # The namespaces of the services WE run, for the resource block on "/admin/diensten".
        # An EXPLICIT list and not a derivation from the keys above: it says what belongs on
        # that page. rig-prd-ron is in it because the mail relay is a platform service and
        # not a project, even though the namespace carries the project prefix. A namespace
        # forgotten here shows up as a block with too little in it, so the page states the
        # number of namespaces itself.
        "service_namespaces": ["rig-prd-operations", "rig-prd-backup", "rig-prd-ron"],
        "mail_relay_port": 587,
        "mail_from_address": "noreply-rijksapp@rijksoverheid.nl",
        # Namespace of the CloudNativePG operator. ODCN installs it outside the CNPG
        # default; tenants cannot see it, the name comes from ODC-Noord (2026-11-04).
        "database_operator_namespace": "quattro-cloudnative-db-operator",
        "ops_egress": OPS_EGRESS,
        "cluster_cidrs": ODCN_CLUSTER_CIDRS,
        "ingress_controller_selector": {
            "namespace": "openshift-ingress",
            "pod_labels": {
                "ingresscontroller.operator.openshift.io/deployment-ingresscontroller": "rig",
            },
        },
        "ingress": {
            "enable_tls": True,
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
        "oom_pod_watch": True,
        # Reachable from the internet and running a real cert-manager, so an ACME HTTP-01
        # challenge for a domain of the user's own can complete here.
        "supports_custom_domain_certificates": True,
        # VLAM (the language-model API of SSC-ICT) is reachable here only: the RON link
        # exists on this cluster and nowhere else. The keys describe WHERE the internal proxy
        # of the vlam project runs; the service derives both the address and the network rule
        # from them, so those two cannot drift apart. ``namespace`` is the unprefixed name out
        # of the project file; the cluster puts ``rig-prd-`` in front of it
        # (get_prefixed_namespace).
        "vlam": {
            "project": "vlam-wt8",
            "deployment": "productie",
            "component": "vlam-proxy-intern",
            "namespace": "vlam-wt8",
            # Port 8082 and not 8081. Both terminate, but 8081 goes to
            # `vlam-api.rijksweb.nl` and SSC-ICT has switched that address off: measured on
            # 2026-09-21 it gives no answer any more (the connection is closed right after the
            # TCP handshake), while `vlam-api.overheid-i.nl` gives 200. 8082 goes to the new
            # address. 8081 stays in the proxy as long as there can be consumers on it, but a
            # new consumer should no longer end up there.
            "port": 8082,
            # THE PASSTHROUGH PATH (RC-167). Port 8443 of the same proxy passes the TLS
            # session through without terminating it: the consumer then talks to VLAM itself
            # and verifies it itself. Three values make that usable, and they belong together:
            #
            #   passthrough_port  where the passthrough listens;
            #   api_host          the name that is in the URL, because TLS compares the host
            #                     name out of the URL with the certificate. Since 2026-09-21
            #                     this is the overheid-i address; the SNI ACL of the
            #                     passthrough already let both names through;
            #   cluster_ip        where that name has to point. hostAliases takes an ADDRESS,
            #                     not a service name, and ZAD's Service template sets no
            #                     clusterIP, so this is the dynamically assigned address of
            #                     `productie-vlam-proxy-intern`. Deliberately NOT pinned: a
            #                     ClusterIP is immutable as long as the Service exists, and if
            #                     it does shift, this fails safely on a certificate error
            #                     instead of delivering to the wrong place. This is the one
            #                     place to change it.
            #
            # No own CA bundle here: the certificate of `vlam-api.overheid-i.nl` comes from
            # DigiCert and sits in every standard bundle, so the consumer verifies it with
            # the certificate list of its own image.
            "passthrough_port": 8443,
            "api_host": "vlam-api.overheid-i.nl",
            "cluster_ip": "172.30.254.144",
        },
        "letsencrypt": {
            "contact_email": "rig-platform@rijksoverheid.nl",  # Default contact for Let's Encrypt certificates
        },
        "domains": {
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
            # The fourth is the ingress_postfix zone above: ours, but not offered. Name the
            # zone itself and never its parent rijksapps.nl: that one is ODC-Noord's and in
            # use as projects' own base domain.
            "managed_zones": ["rijks.app", "rijksapp.nl", "rijksapp.dev", "rig.prd1.gn2.quattro.rijksapps.nl"],
        },
        # Behind a Quay operator: a private registry becomes a proxy organisation in RCR and
        # the image is rewritten. The rules below are the shared proxy caches of the platform;
        # project rules come in front of them.
        "image_registries": {
            "backend": "quay-proxy-organization",
            "registry_host": "rcr.rijksapps.nl",
            "customer_name": "rig",
            # The Capsule tenants the operator puts the pull secret in. A required field on
            # the CR; our namespaces are called rig-prd-*, so the tenant is prd.
            "tenants": ["prd"],
            # Measured on the cluster, not out of the operator documentation; see
            # features/image-registries.md, "De provisioning-backend".
            "organization_api_version": "quay.k8s.rijksapps.nl/v1alpha1",
            "rotation_days": 90,
            # 10, not 3: a proxy cache keeps every tag it pulls, and 3 GiB filled up within a
            # few builds (pocpl-0g8, November 2026). Counts against rig-quay-storage-quota.
            "quota_limit_gib": 10,
            "rules": [
                {"match": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig", "secret": "ghcr-rig-robot-pull-secret"},
                {
                    "match": "docker.io",
                    "to": "rcr.rijksapps.nl/dockerhub-rig",
                    "secret": "dockerhub-rig-robot-pull-secret",
                },
                {
                    "match": "registry.gitlab.com",
                    "to": "rcr.rijksapps.nl/gitlab-rig",
                    "secret": "gitlab-rig-robot-pull-secret",
                },
                {"match": "gcr.io", "to": "rcr.rijksapps.nl/gcr-rig", "secret": "gcr-rig-robot-pull-secret"},
                {"match": "quay.io", "to": "rcr.rijksapps.nl/quay-rig", "secret": "quay-rig-robot-pull-secret"},
                {
                    "match": "registry.k8s.io",
                    "to": "rcr.rijksapps.nl/k8s-rig",
                    "secret": "k8s-rig-robot-pull-secret",
                },
                {
                    "match": "code.overheid.nl",
                    "to": "rcr.rijksapps.nl/code-overheid-rig",
                    "secret": "code-overheid-rig-robot-pull-secret",
                },
            ],
        },
    },
    # The same OpenShift cluster as odcn-production, in the Capsule tenant rig-staging: the
    # pentest environment, see features/zad-staging.md. What the cluster itself decides
    # (router, storage, Quay) is equal to production; mail and VLAM come from the sandbox,
    # because the RON link exists for production only.
    "odcn-staging": {
        # The same router shard as production, so the same technical zone as well.
        "ingress_postfix": ".rig.prd1.gn2.quattro.rijksapps.nl",
        "namespace_prefix": "rig-staging-",
        "namespace": "rig-staging-operations",
        "argo_namespace": "rig-staging-operations",
        "keycloak_discovery_url": "https://keycloak.staging.rijksapp.dev",
        "database_server": "rig-db-rw.rig-staging-operations.svc.cluster.local",
        "minio_host": "minio.rig-staging-operations.svc.cluster.local",
        "minio_port": MINIO_PORT,
        "seaweedfs_host": "seaweedfs.rig-staging-operations.svc.cluster.local",
        "seaweedfs_port": SEAWEEDFS_S3_PORT,
        "redis_server": "rig-redis.rig-staging-operations.svc.cluster.local",
        "backup_namespace": "rig-staging-backup",
        # A relay with the mail sink of the sandbox: no mail goes out.
        "mail_relay_namespace": "rig-staging-ron",
        "mail_relay_host": "rig-mail-relay.rig-staging-ron.svc.cluster.local",
        "service_namespaces": ["rig-staging-operations", "rig-staging-backup", "rig-staging-ron"],
        "mail_relay_port": 587,
        "mail_from_address": "noreply-rijksapp@rijksoverheid.nl",
        # Same cluster as production, so the same operator namespace.
        "database_operator_namespace": "quattro-cloudnative-db-operator",
        "ops_egress": OPS_EGRESS,
        # Staging runs on the same cluster as production, so the same CIDRs.
        "cluster_cidrs": ODCN_CLUSTER_CIDRS,
        "ingress_controller_selector": {
            "namespace": "openshift-ingress",
            "pod_labels": {
                "ingresscontroller.operator.openshift.io/deployment-ingresscontroller": "rig",
            },
        },
        "ingress": {
            "enable_tls": True,
            "ip_whitelist": "0.0.0.0/0,::/0",
        },
        "storage": {
            "storage_class_name": "ocs-storagecluster-ceph-rbd",
            "access_modes": ["ReadWriteOnce"],
            "volume_snapshot_class": "ocs-storagecluster-rbdplugin-snapclass",
        },
        "keycloak": {
            "support_http": False,
        },
        "min_memory_limit_mi": 25,
        "max_memory_limit_mi": 4096,
        "max_memory_request_mi": 1024,
        "uses_capsule": True,
        "min_cpu_m": 25,
        "max_cpu_request_m": 250,
        "max_cpu_limit_m": 4000,
        "supports_vpa": True,
        "oom_pod_watch": True,
        "supports_custom_domain_certificates": True,
        # A PLACEHOLDER, as in the sandbox: there is no VLAM on staging. Only the wiring of
        # the service can be walked end to end this way; the address points at a project that
        # does not exist here.
        "vlam": {
            "project": "vlam-wt8",
            "deployment": "productie",
            "component": "vlam-proxy-intern",
            "namespace": "vlam-wt8",
            "port": 8081,
            "passthrough_port": 8443,
            "api_host": "vlam-api.overheid-i.nl",
            "cluster_ip": "172.30.0.1",
        },
        "letsencrypt": {
            "contact_email": "rig-platform@rijksoverheid.nl",
        },
        "domains": {
            "supported_domains": [
                {
                    "domain": "staging.rijksapp.dev",
                    "supports_dots": True,
                    "issuer": "letsencrypt",
                    "restricted_subdomains": True,
                    "external_dns_target": "router.rijksapp.dev",
                },
            ],
            "managed_zones": ["staging.rijksapp.dev", "rig.prd1.gn2.quattro.rijksapps.nl"],
        },
        "image_registries": {
            "backend": "quay-proxy-organization",
            "registry_host": "rcr.rijksapps.nl",
            "customer_name": "rig",
            "tenants": ["staging"],
            "organization_api_version": "quay.k8s.rijksapps.nl/v1alpha1",
            "rotation_days": 90,
            "quota_limit_gib": 3,
            # The shared proxy caches of the customer rig, the same as in production.
            "rules": [
                {"match": "ghcr.io", "to": "rcr.rijksapps.nl/ghcr-rig", "secret": "ghcr-rig-robot-pull-secret"},
                {
                    "match": "docker.io",
                    "to": "rcr.rijksapps.nl/dockerhub-rig",
                    "secret": "dockerhub-rig-robot-pull-secret",
                },
                {
                    "match": "registry.gitlab.com",
                    "to": "rcr.rijksapps.nl/gitlab-rig",
                    "secret": "gitlab-rig-robot-pull-secret",
                },
                {"match": "gcr.io", "to": "rcr.rijksapps.nl/gcr-rig", "secret": "gcr-rig-robot-pull-secret"},
                {"match": "quay.io", "to": "rcr.rijksapps.nl/quay-rig", "secret": "quay-rig-robot-pull-secret"},
                {
                    "match": "registry.k8s.io",
                    "to": "rcr.rijksapps.nl/k8s-rig",
                    "secret": "k8s-rig-robot-pull-secret",
                },
                {
                    "match": "code.overheid.nl",
                    "to": "rcr.rijksapps.nl/code-overheid-rig",
                    "secret": "code-overheid-rig-robot-pull-secret",
                },
            ],
        },
    },
}


def get_cluster_config(cluster_name: str) -> dict:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
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

    manager = settings.CLUSTER_MANAGER
    configured = get_cluster_config(manager).get("create_wizard_clusters", [manager])
    return [c for c in configured if c in CLUSTER_CONFIG]


def get_ingress_postfix(cluster_name: str) -> str:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["ingress_postfix"]


def get_namespace_prefix(cluster_name: str) -> str:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["namespace_prefix"]


def get_service_namespaces(cluster_name: str) -> list[str]:
    """The namespaces the platform runs its OWN services in.

    Used by the resource block on "/admin/diensten". A cluster without this key gives an empty
    list and the block then says nothing is configured; that is better than falling back on
    "everything", because the page would then quietly show user projects.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return list(cluster_config.get("service_namespaces", []))


def get_external_clone_allowed_private_networks(cluster_name: str) -> list[str]:
    """The private ranges an external clone of this cluster may connect to after all.

    None by default: a clone source belongs on a public address. A cluster that needs a range
    puts it here with the reason next to it. A project cannot set this.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return list(cluster_config.get("external_clone_allowed_private_networks", []))


def get_argo_namespace(cluster_name: str) -> str:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["argo_namespace"]


def get_prefixed_namespace(cluster_name: str, namespace: str) -> str:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    prefix = get_namespace_prefix(cluster_name)
    return f"{prefix}{namespace}"


# Fixed prefixes of namespaces that are not ours, but that a project may not land in
# either.
RESERVED_NAMESPACE_PREFIXES = ("kube-", "openshift-", "cnpg-")

# The keys in CLUSTER_CONFIG that point at a platform namespace.
_PLATFORM_NAMESPACE_KEYS = (
    "namespace",
    "argo_namespace",
    "backup_namespace",
    "mail_relay_namespace",
    "database_operator_namespace",
)


def get_platform_namespaces() -> frozenset[str]:
    """The namespaces the platform itself runs in, over ALL clusters.

    Derived from CLUSTER_CONFIG and not from a second list: a platform namespace added there
    is reserved at once instead of only after an edit here.
    """
    namespaces: set[str] = set()
    for cluster_config in CLUSTER_CONFIG.values():
        for key in _PLATFORM_NAMESPACE_KEYS:
            value = cluster_config.get(key)
            if isinstance(value, str) and value:
                namespaces.add(value)
        namespaces.update(str(ns) for ns in cluster_config.get("service_namespaces", []))
    return frozenset(namespaces)


def is_platform_namespace(namespace: str) -> bool:
    """True when this namespace is the platform's and therefore never a project's."""
    return namespace in get_platform_namespaces() or namespace.startswith(RESERVED_NAMESPACE_PREFIXES)


def is_reserved_project_name(name: str) -> bool:
    """True when the namespace of this project is a platform namespace on any cluster.

    The namespace is the cluster prefix plus the project name, so a name like ``operations``
    or ``system`` ends up on the namespace of the platform itself.
    """
    if not name:
        return False
    return any(is_platform_namespace(get_prefixed_namespace(cluster, name)) for cluster in CLUSTER_CONFIG)


def get_storage_config(cluster_name: str) -> dict:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("storage", {"storage_class_name": "standard", "access_modes": ["ReadWriteOnce"]})


def get_storage_class_name(cluster_name: str) -> str:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    storage_config = get_storage_config(cluster_name)
    return storage_config["storage_class_name"]


def get_storage_access_modes(cluster_name: str) -> list[str]:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    storage_config = get_storage_config(cluster_name)
    return storage_config["access_modes"]


def get_volume_snapshot_class(cluster_name: str) -> str | None:
    """Used when creating VolumeSnapshots for PVC backups. None when the cluster configures
    none.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    storage_config = get_storage_config(cluster_name)
    return storage_config.get("volume_snapshot_class")


def get_ingress_config(cluster_name: str) -> dict:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("ingress", {"enable_tls": False, "ip_whitelist": "0.0.0.0/0,::/0"})


def get_ingress_tls_enabled(cluster_name: str) -> bool:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    ingress_config = get_ingress_config(cluster_name)
    return ingress_config["enable_tls"]


def get_ingress_ip_whitelist(cluster_name: str) -> str:
    """The IP whitelist for ingresses on this cluster, in CIDR notation.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    ingress_config = get_ingress_config(cluster_name)
    return ingress_config["ip_whitelist"]


def get_ingress_cluster_issuer(cluster_name: str) -> str | None:
    """The cert-manager ClusterIssuer for TLS certificates, None when the cluster configures
    none.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    ingress_config = get_ingress_config(cluster_name)
    return ingress_config.get("cluster_issuer")


def get_keycloak_discovery_url(cluster_name: str) -> str:
    """The discovery URL pods use to reach Keycloak internally.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["keycloak_discovery_url"]


def get_keycloak_config(cluster_name: str) -> dict:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("keycloak", {"support_http": False})


def get_keycloak_support_http(cluster_name: str) -> bool:
    """True when Keycloak clients get both HTTP and HTTPS redirect URIs, False for HTTPS only.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    keycloak_config = get_keycloak_config(cluster_name)
    return keycloak_config.get("support_http", False)


def get_keycloak_demo_users(cluster_name: str) -> bool:
    """Whether realm blueprints may create their demo users on this cluster.

    Those users carry a password from the repository, so they exist only on a local or
    sandbox cluster. See ``load_demo_users`` in ``opi/manager/keycloak_manager.py``.
    """
    keycloak_config = get_keycloak_config(cluster_name)
    return keycloak_config.get("demo_users", False)


def get_database_server(cluster_name: str) -> str:
    """The hostname pods use to reach the PostgreSQL database internally.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["database_server"]


def get_minio_host(cluster_name: str) -> str:
    """The minio hostname pods use internally.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["minio_host"]


def get_minio_port(cluster_name: str) -> int:
    """The minio port pods use internally.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["minio_port"]


def get_seaweedfs_host(cluster_name: str) -> str | None:
    """The SeaweedFS address of this cluster, or None when it does not offer that service.

    As with ``get_vlam_config`` this is the whole availability question, so the service
    itself names no cluster. It also decides whether the tenant baseline gets an egress rule
    to the S3 port, see :func:`get_ops_egress`.
    """
    return get_cluster_config(cluster_name).get("seaweedfs_host")


def get_seaweedfs_port(cluster_name: str) -> int:
    """The S3 port of SeaweedFS on this cluster."""
    return get_cluster_config(cluster_name)["seaweedfs_port"]


def get_minio_server(cluster_name: str) -> str:
    """The minio address (host:port) pods use internally.

    For the two parts apart: get_minio_host() and get_minio_port().

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    return f"{get_minio_host(cluster_name)}:{get_minio_port(cluster_name)}"


def get_namespace(cluster_name: str) -> str:
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["namespace"]


def get_backup_namespace(cluster_name: str) -> str:
    """Where the cluster's backup MinIO (or comparable) lives, so workloads can reach it for
    restores and snapshots. NetworkPolicies use this as an allowed peer namespace.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["backup_namespace"]


def get_database_operator_namespace(cluster_name: str) -> str:
    """Namespace of the CloudNativePG operator for a cluster.

    A dedicated (project-scoped) PostgreSQL cluster lives in the project's infrastructure
    namespace, but the CNPG operator that manages it runs in its own namespace and must reach
    the cluster's pods to extract their instance status. Without an ingress allowance for this
    namespace the operator reports "Instance Status Extraction Error", the Cluster never
    becomes Ready and its ArgoCD health stays Unknown. The infra-namespace NetworkPolicy uses
    this as an allowed peer.

    Defaults to ``cnpg-system`` (the CloudNativePG default install namespace) for clusters that
    predate this setting.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("database_operator_namespace", "cnpg-system")


def get_ops_egress(cluster_name: str) -> list[dict[str, Any]]:
    """Shared services in the operations namespace per port, for the tenant baseline.

    A cluster that offers seaweedfs-storage gets the S3 port of that service added; that
    follows from ``seaweedfs_host`` and is not in the list per cluster.
    """
    config = get_cluster_config(cluster_name)
    egress: list[dict[str, Any]] = list(config["ops_egress"])
    if config.get("seaweedfs_host"):
        egress.append(SEAWEEDFS_OPS_EGRESS)
    return egress


def get_cluster_cidrs(cluster_name: str) -> dict[str, list[str]] | None:
    """Pod and service CIDRs for the except of the internet rule, or None."""
    cidrs = get_cluster_config(cluster_name).get("cluster_cidrs")
    if not cidrs:
        logger.warning(
            f"Cluster '{cluster_name}' heeft geen cluster_cidrs: de internetregel van de tenant-baseline "
            "krijgt geen except en opent daarmee ook pods in andere namespaces op 80 en 443"
        )
        return None
    return cidrs


def get_ingress_controller_selector(cluster_name: str) -> dict:
    """The selector for the ingress controller pods of a cluster.

    Shaped ``{"namespace": "<ns>", "pod_labels": {<key>: <val>, ...}}``.

    pod_labels is empty for nginx clusters; for odcn (OpenShift Router) it holds the
    ``ingresscontroller.operator.openshift.io/deployment-ingresscontroller`` label, so the
    NetworkPolicy admits the right customer router only. See
    docs/knowledge/odcn-ingress-controller.md.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["ingress_controller_selector"]


def get_redis_server(cluster_name: str) -> str:
    """The hostname pods use to reach Redis internally.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["redis_server"]


def get_mail_relay_namespace(cluster_name: str) -> str:
    """The namespace the SMTP relay runs in.

    Its own namespace and not the operations namespace: the Calico annotation
    ``egress.projectcalico.org/egressGatewayPolicy`` takes exactly ONE value, so a namespace can
    have RON egress or internet egress, never both. The operations namespace needs internet
    (ArgoCD, the registry, Keycloak), so the relay lives apart. See ``plans/mailrelay.md``.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["mail_relay_namespace"]


def get_mail_relay_host(cluster_name: str) -> str:
    """The in-cluster hostname of the SMTP relay.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["mail_relay_host"]


def get_mail_relay_port(cluster_name: str) -> int:
    """The submission port of the SMTP relay.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["mail_relay_port"]


def get_mail_from_address(cluster_name: str) -> str:
    """The BASE sender address of this cluster: the bare one, without a plus part.

    Not what a project sends from -- that is ``<local>+<project>@<domain>``, composed by
    ``generate_mail_sender_address`` and handed to the relay by ``MailManager``. What lives
    here is the pair the relay itself is configured with (MAIL_FROM_LOCAL and MAIL_DOMAIN
    in its secret), so composing happens in ONE place instead of here as well.

    It is also the FALLBACK: a message from an account the relay holds no sender for goes
    out under exactly this address, without a display name. So is the mail from ZAD's own
    platform account, which is not a project and has no plus part to fill.

    And so is KEYCLOAK's login mail: it leaves under exactly this address and tells itself
    apart from the portal's post by its display name (``MAIL_KEYCLOAK_FROM_NAME``) alone. A
    bounce therefore cannot be traced back to login mail rather than portal mail.

    OPI and the relay must agree on this value -- if they drift, a developer is shown one
    address while another one leaves the building. For login mail the same value is written
    down in THREE places that have to agree: the relay gets it as the sender of
    ``zad-keycloak`` (``MailManager.ensure_keycloak_account``), the Keycloak pod gets it as
    ``ZAD_MAIL_RELAY_FROM``, and OPI writes it into every realm's minimal ``smtpServer``.
    Drift between them shows up as a message that leaves under one address while a realm
    claims another, so it is pinned by a test rather than by care
    (``test_de_drie_plekken_noemen_hetzelfde_afzenderadres``).

    It is a domain we do NOT own: mail goes out over the Rijksoverheid mail server, so it
    carries their domain. That is also the only arrangement that survives DMARC, because
    they publish ``p=reject`` and we sign nothing with DKIM, leaving SPF alignment between
    envelope and ``From:`` as the single thing that can pass. See docs/operations/ron-koppeling.md.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config["mail_from_address"]


def get_infrastructure_namespace(cluster_name: str, project_name: str) -> str:
    """The base infrastructure namespace name of a project with the cluster's namespace prefix
    in front of it.

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
    """The database service endpoint with the infrastructure namespace in it, so DNS resolves
    across namespaces.

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
    """The floor below which the container runtime does not accept a memory limit."""
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("min_memory_limit_mi", 25)


def get_max_memory_limit_mi(cluster_name: str) -> int:
    """The upper bound for auto-tuning and for the wizard dropdowns."""
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("max_memory_limit_mi", 4096)


def get_max_memory_request_mi(cluster_name: str) -> int:
    """Memory requests are capped lower than limits: below this cap requests and limits scale
    together, above it only limits can increase further.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("max_memory_request_mi", 1024)


def uses_capsule(cluster_name: str) -> bool:
    """With Capsule, namespace creation has to wait for Capsule to assign the tenant label
    before modifications can be made.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("uses_capsule", False)


def get_pod_security_labels(cluster_name: str) -> dict[str, str]:
    """Pod Security Admission labels for this cluster, or {} if not configured."""
    cluster_config = get_cluster_config(cluster_name)
    modes: dict[str, str] = cluster_config.get("pod_security", {})
    return {f"pod-security.kubernetes.io/{mode}": level for mode, level in modes.items()}


def namespace_labels(cluster_name: str) -> dict[str, str]:
    """The labels every namespace OPI manages must carry on this cluster."""
    return {
        "argocd.argoproj.io/managed-by": get_argo_namespace(cluster_name),
        **get_pod_security_labels(cluster_name),
    }


def supports_vpa(cluster_name: str) -> bool:
    """When True the platform runs a VPA recommender that publishes recommendations to
    VerticalPodAutoscaler objects, and auto-tuning sources memory and CPU from VPA instead of
    from Prometheus.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("supports_vpa", False)


def watches_pods_for_oom(cluster_name: str) -> bool:
    """Whether this cluster runs the cluster-wide pod watch that catches runtime OOM kills.

    Off everywhere until the watch has proven itself on one cluster: it opens a long-lived
    stream over every application pod and remediates on what it sees, so it is switched on per
    cluster rather than by being merged.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("oom_pod_watch", False)


def supports_custom_domain_certificates(cluster_name: str) -> bool:
    """Whether this cluster can obtain a certificate for a domain of the user's own.

    A domain outside the cluster's ``domains.supported_domains`` gets no certificate for
    free: the platform certificate covers the supported domains only, so cert-manager has
    to issue one, over an ACME HTTP-01 challenge that the outside world must be able to
    reach. On production that works. On the two Kind clusters it cannot: they are not
    reachable from the internet, and ``task sandbox:setup`` even installs a FAKE
    cert-manager CRD (``bootstrap/crd/cert-manager/fake-cert-manager.yaml``) with no
    controller behind it, so the Issuer applies, reports Ready, and nothing is ever
    issued. Everything stays green and the site serves the wrong certificate.

    So this is a capability of the cluster and not a property of the domain, and it says
    only what the platform will do, never whether the DNS or the ownership is in order.

    Absent means True: silence is the right answer for a cluster that has not declared
    this, since a warning nobody configured would be a guess about someone else's cluster.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("supports_custom_domain_certificates", True)


def get_min_cpu_m(cluster_name: str) -> int:
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("min_cpu_m", 25)


def get_max_cpu_request_m(cluster_name: str) -> int:
    """Auto-tuning never sets a CPU request above this ceiling; a pod that genuinely needs more
    requires manual intervention.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("max_cpu_request_m", 250)


def get_max_cpu_limit_m(cluster_name: str) -> int:
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("max_cpu_limit_m", 4000)


def get_letsencrypt_contact_email(cluster_name: str) -> str | None:
    """Used for the ACME account registration and for certificate expiry notifications. A
    project can override it with its own contact-email in the project config.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    letsencrypt_config = cluster_config.get("letsencrypt", {})
    return letsencrypt_config.get("contact_email")


#: Hashes that were found, keyed by certificate path. The certificate does not change during
#: the lifetime of the process, and without a cache ``_compute_ca_hash`` started openssl per
#: component, on the event loop. Only a hash that was found goes in: caching a failure would
#: make a certificate that appears later unreadable forever.
_ca_hash_cache: dict[str, str] = {}


def _compute_ca_hash(cert_path: str) -> str | None:
    """The 8-character OpenSSL hash of a CA certificate, or None when it cannot be computed.

    OpenSSL uses this hash for directory-based CA lookup.
    """
    if cert_path in _ca_hash_cache:
        return _ca_hash_cache[cert_path]

    if not Path(cert_path).exists():
        return None

    try:
        result = subprocess.run(  # noqa: S603  # no shell; the variable parts are separate argv elements
            ["openssl", "x509", "-hash", "-noout", "-in", cert_path],  # noqa: S607  # a tool out of the image PATH, not a fixed path
            capture_output=True,
            text=True,
            check=True,
            env=child_env(),
        )
    except subprocess.CalledProcessError, FileNotFoundError:
        return None

    ca_hash = result.stdout.strip()
    if ca_hash:
        _ca_hash_cache[cert_path] = ca_hash
    return ca_hash


def get_ca_certificate_config(cluster_name: str) -> dict | None:
    """The CA certificate configuration, enriched with the OpenSSL hash for directory-based
    lookup. None when the cluster has no CA certificate configured.

    The keys: ``enabled``, ``node_path`` (the path on the Kubernetes node), ``container_path``
    (where it is mounted in containers), ``hash`` (computed at runtime) and ``env_vars`` (what
    to set for the SSL libraries).

    Raises ``ValueError`` when the cluster is not in the configuration.
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


def get_cluster_domains_config(cluster_name: str) -> dict | None:
    """The ``domains`` block, with one ``supported_domains`` entry per domain the cluster
    offers. None when the cluster offers no domains of its own.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    cluster_config = get_cluster_config(cluster_name)
    return cluster_config.get("domains")


def get_supported_domain_names(cluster_name: str) -> list[str]:
    """The ``domain`` of every entry in ``supported_domains``, empty when the cluster offers no
    domains of its own.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    domains_config = get_cluster_domains_config(cluster_name)
    if domains_config is None:
        return []
    raw = domains_config.get("supported_domains", [])
    return [entry["domain"] if isinstance(entry, dict) else entry for entry in raw]


def is_domain_supported(cluster_name: str, base_domain: str) -> bool:
    """Raises ``ValueError`` when the cluster is not in the configuration."""
    supported_domains = get_supported_domain_names(cluster_name)
    return base_domain in supported_domains


def get_managed_zones(cluster_name: str) -> list[str]:
    """The ``managed_zones`` of the cluster, empty when it declares none.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    domains_config = get_cluster_domains_config(cluster_name)
    if domains_config is None:
        return []
    return list(domains_config.get("managed_zones", []))


def _longest_matching_zone(hostname: str, zones: list[str]) -> str | None:
    """Return the most specific zone the hostname falls under, or None."""
    for zone in sorted(zones, key=len, reverse=True):
        if hostname == zone or hostname.endswith("." + zone):
            return zone
    return None


def is_platform_domain(cluster_name: str, domain: str) -> bool:
    """Whether a domain falls within a zone this cluster serves.

    This is the management question, not the offer question ``is_domain_supported`` answers:
    ``team.rijks.app`` is not offered and is still ours.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    return _longest_matching_zone(domain, get_managed_zones(cluster_name)) is not None


def get_domain_issuer(cluster_name: str, domain: str) -> str | None:
    """The per-domain issuer out of the cluster's ``supported_domains``, falling back to the
    cluster's default ``cluster_issuer``. None when no issuer is needed.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    domains_config = get_cluster_domains_config(cluster_name)
    if domains_config is not None:
        for entry in domains_config.get("supported_domains", []):
            if isinstance(entry, dict) and entry.get("domain") == domain:
                return entry.get("issuer")
    return None


def get_external_dns_target_for_hostname(cluster_name: str, hostname: str) -> str | None:
    """The ``external_dns_target`` of the most specific of the cluster's ``supported_domains``
    that the hostname falls under.

    None when no configured domain matches, as for a hostname in the cluster's default postfix
    zone: that gets its DNS through the OpenShift router and needs no explicit target.
    """
    domains_config = get_cluster_domains_config(cluster_name)
    if domains_config is None:
        return None

    candidates = {
        entry["domain"]: entry["external_dns_target"]
        for entry in domains_config.get("supported_domains", [])
        if isinstance(entry, dict) and entry.get("external_dns_target")
    }
    zone = _longest_matching_zone(hostname, list(candidates))
    return candidates[zone] if zone is not None else None


def is_domain_subdomain_restricted(cluster_name: str, domain: str) -> bool:
    """When a domain restricts subdomains, only the subdomains the project lists in its
    allowed-subdomains section may be used.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    domains_config = get_cluster_domains_config(cluster_name)
    if domains_config is None:
        return False
    for entry in domains_config.get("supported_domains", []):
        if isinstance(entry, dict) and entry.get("domain") == domain:
            return entry.get("restricted_subdomains", False)
    return False


def get_restricted_subdomain_domains(cluster_name: str) -> list[str]:
    """The domains on this cluster with ``restricted_subdomains`` on.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    domains_config = get_cluster_domains_config(cluster_name)
    if domains_config is None:
        return []
    return [
        entry["domain"]
        for entry in domains_config.get("supported_domains", [])
        if isinstance(entry, dict) and entry.get("restricted_subdomains", False)
    ]


def get_domain_supports_dots(cluster_name: str, domain: str) -> bool:
    """Whether a domain accepts dot-separated hostnames.

    Raises ``ValueError`` when the cluster is not in the configuration.
    """
    domains_config = get_cluster_domains_config(cluster_name)
    if domains_config is None:
        return False
    for entry in domains_config.get("supported_domains", []):
        if isinstance(entry, dict) and entry.get("domain") == domain:
            return entry.get("supports_dots", False)
    return False


def get_image_registries_config(cluster_name: str) -> dict[str, Any]:
    """The platform facts the service ``image-registries`` needs on this cluster.

    A cluster without the key gets ``direct-secret`` without a table.
    """
    try:
        config = get_cluster_config(cluster_name).get("image_registries")
    except ValueError:
        # The cluster check sits in validate_service_availability and in the schema; blowing
        # up here would make a missing cluster field arrive as a registry error.
        return {"backend": "direct-secret"}
    if not isinstance(config, dict):
        return {"backend": "direct-secret"}
    return config


def get_vlam_config(cluster_name: str) -> dict[str, Any] | None:
    """Where the in-cluster VLAM proxy runs on this cluster, or None when there is none.

    Absent is the normal answer: VLAM hangs off the RON link, which exists on exactly one
    cluster. A cluster without this key neither offers the ``vlam`` service in the wizard
    nor accepts a project that selected it -- that is the whole availability mechanism,
    and it lives here so the service itself names no cluster.

    Keys: ``project`` / ``deployment`` / ``component`` / ``namespace`` (unprefixed) of the
    proxy, plus its ``port``. The address a consumer gets and the NetworkPolicy peer it is
    allowed to reach are BOTH derived from these, so they cannot drift apart.

    The same entry carries the passthrough half: ``passthrough_port``, ``api_host`` and
    ``cluster_ip``. Those three are one unit, because an address without the name it must be
    reached under is not a usable path; ``vlam_endpoint()`` derives them together.
    """
    return get_cluster_config(cluster_name).get("vlam")
