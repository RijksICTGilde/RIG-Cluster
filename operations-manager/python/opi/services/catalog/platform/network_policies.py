"""The namespace-wide NetworkPolicies of a project namespace.

They belong to the project and not to one deployment, so the platform service contributes
them to the project level. See features/restrictive-network-policies.md.
"""

from __future__ import annotations

from typing import Any

from opi.core.cluster_config import (
    get_backup_namespace,
    get_cluster_cidrs,
    get_database_operator_namespace,
    get_ingress_controller_selector,
    get_namespace,
    get_ops_egress,
)
from opi.services.catalog.base import ProjectManifestSpec
from opi.services.services_enums import ServiceType
from opi.utils.naming import (
    generate_network_policy_manifest_name,
    generate_network_policy_name,
    get_infrastructure_namespace,
)

#: The purposes of the three policies, in the order they are written.
NAMESPACE_POLICY_PURPOSES = ("default-deny", "unlabeled-baseline", "dedicated-database")


def legacy_deployment_policy_filenames() -> list[str]:
    """The filenames these policies carried in a deployment directory before RC-503."""
    return [f"{generate_network_policy_manifest_name(purpose)}.yaml" for purpose in NAMESPACE_POLICY_PURPOSES]


def namespace_network_policy_values(*, cluster: str, project_name: str, namespace: str) -> dict[str, dict[str, Any]]:
    """The template values per purpose: default-deny, the unlabeled baseline, the database access.

    ``prune_disabled`` renders ``Prune=false``: while a project moves these out of its
    deployment directory, that deployment's application loses them from git before the
    project application has adopted them, and would otherwise delete them (RC-503).
    """
    return {
        "default-deny": {
            "name": generate_network_policy_name("default-deny"),
            "namespace": namespace,
            "prune_disabled": True,
        },
        "unlabeled-baseline": {
            "name": generate_network_policy_name("unlabeled-baseline"),
            "namespace": namespace,
            "prune_disabled": True,
            # Not a deployment_selector but its counterpart: exactly the pods without the
            # label, with the same rights as a deployment.
            "deployment_selector": None,
            "unlabeled_selector": True,
            "ops_namespace": get_namespace(cluster),
            "backup_namespace": get_backup_namespace(cluster),
            "ingress_controller_selector": get_ingress_controller_selector(cluster),
            "ops_egress": get_ops_egress(cluster),
            "cluster_cidrs": get_cluster_cidrs(cluster),
            # The operator reaches the database cluster through the policy below, not through
            # the baseline: it does not need the helm pods in this namespace.
            "database_operator_namespace": None,
            "project_infra_namespace": get_infrastructure_namespace(cluster, project_name),
        },
        "dedicated-database": {
            "name": generate_network_policy_name("dedicated-database"),
            "namespace": namespace,
            "prune_disabled": True,
            "database_operator_namespace": get_database_operator_namespace(cluster),
        },
    }


#: Which template renders which purpose.
NAMESPACE_POLICY_TEMPLATES = {
    "default-deny": "default-deny-network-policy.yaml.jinja",
    "unlabeled-baseline": "tenant-baseline-network-policy.yaml.jinja",
    "dedicated-database": "dedicated-database-network-policy.yaml.jinja",
}


def namespace_network_policy_specs(*, cluster: str, project_name: str, namespace: str) -> list[ProjectManifestSpec]:
    """The three policies as project-level manifests, with the platform filename prefix."""
    values = namespace_network_policy_values(cluster=cluster, project_name=project_name, namespace=namespace)
    return [
        ProjectManifestSpec(
            filename=f"{ServiceType.PLATFORM.value}-{generate_network_policy_manifest_name(purpose)}",
            template_path=NAMESPACE_POLICY_TEMPLATES[purpose],
            values=values[purpose],
        )
        for purpose in NAMESPACE_POLICY_PURPOSES
    ]
