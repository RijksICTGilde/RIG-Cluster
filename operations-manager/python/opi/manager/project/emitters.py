"""Stateless manifest emitters shared by the Helm paths and the manifest generation."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from opi.core.cluster_config import (
    get_backup_namespace,
    get_cluster_cidrs,
    get_database_operator_namespace,
    get_infrastructure_namespace,
    get_ingress_controller_selector,
    get_letsencrypt_contact_email,
    get_namespace,
    get_ops_egress,
)
from opi.generation.manifests import MANIFEST_TEMPLATES_DIR
from opi.services.project_utils import namespace_level_deployment
from opi.utils.naming import (
    generate_issuer_manifest_name,
    generate_issuer_name,
    generate_issuer_secret_name,
    generate_network_policy_manifest_name,
    generate_network_policy_name,
)

if TYPE_CHECKING:
    from opi.generation.manifests import ManifestGenerator

logger = logging.getLogger(__name__)


def emit_acme_network_policy(
    manifest_generator: ManifestGenerator,
    *,
    deployment_name: str,
    namespace: str,
    cluster_name: str,
    output_dir: str,
) -> str:
    """NetworkPolicy for cert-manager's HTTP-01 solver: port 8089, from the cluster router only."""
    template_path = os.path.join(MANIFEST_TEMPLATES_DIR, "network-policy.yaml.jinja")
    manifest_name = generate_network_policy_manifest_name("acme-http", deployment_name)
    path = manifest_generator.create_manifest_file(
        template_path=template_path,
        values={
            "name": generate_network_policy_name("acme-http", deployment_name),
            "namespace": namespace,
            "pod_selector": 'acme.cert-manager.io/http01-solver: "true"',
            "from_selector": get_ingress_controller_selector(cluster_name),
            "ports": [8089],
        },
        output_dir=output_dir,
        output_filename=manifest_name,
        use_sops=False,
    )
    logger.info(f"Created ACME HTTP-01 solver network policy: {path}")
    return f"{manifest_name}.yaml"


def emit_namespace_network_policies(
    manifest_generator: ManifestGenerator,
    *,
    project_data: dict[str, Any],
    project_name: str,
    deployment: dict[str, Any],
    cluster_name: str,
    namespace: str,
    output_dir: str,
) -> list[str]:
    """Create or clean up namespace-level NetworkPolicies (default-deny, unlabeled-baseline, dedicated-database)."""
    default_deny_name = generate_network_policy_manifest_name("default-deny")
    unlabeled_name = generate_network_policy_manifest_name("unlabeled-baseline")
    database_name = generate_network_policy_manifest_name("dedicated-database")

    owner = namespace_level_deployment(project_data.get("deployments", []), cluster_name, deployment["namespace"])
    if owner is None or owner["name"] != deployment["name"]:
        for filename in (default_deny_name, unlabeled_name, database_name):
            stale = os.path.join(output_dir, f"{filename}.yaml")
            if os.path.exists(stale):
                os.remove(stale)
                logger.info(f"Removed namespace network policy '{filename}' from non-owner '{deployment['name']}'")
        return []

    os.makedirs(output_dir, exist_ok=True)
    baseline_values = {
        "name": generate_network_policy_name("unlabeled-baseline"),
        "namespace": namespace,
        # Not a deployment_selector but its counterpart: exactly the pods without the
        # label, with the same rights as a deployment.
        "deployment_selector": None,
        "unlabeled_selector": True,
        "ops_namespace": get_namespace(cluster_name),
        "backup_namespace": get_backup_namespace(cluster_name),
        "ingress_controller_selector": get_ingress_controller_selector(cluster_name),
        "ops_egress": get_ops_egress(cluster_name),
        "cluster_cidrs": get_cluster_cidrs(cluster_name),
        # The operator reaches the database cluster through the policy below, not through
        # the baseline: it does not need the helm pods in this namespace.
        "database_operator_namespace": None,
        "project_infra_namespace": get_infrastructure_namespace(cluster_name, project_name),
    }
    specs = (
        (
            default_deny_name,
            "default-deny-network-policy.yaml.jinja",
            {"name": generate_network_policy_name("default-deny"), "namespace": namespace},
        ),
        (unlabeled_name, "tenant-baseline-network-policy.yaml.jinja", baseline_values),
        (
            database_name,
            "dedicated-database-network-policy.yaml.jinja",
            {
                "name": generate_network_policy_name("dedicated-database"),
                "namespace": namespace,
                "database_operator_namespace": get_database_operator_namespace(cluster_name),
            },
        ),
    )
    created_files = []
    for filename, template_name, values in specs:
        manifest_generator.create_manifest_file(
            template_path=os.path.join(MANIFEST_TEMPLATES_DIR, template_name),
            values=values,
            output_dir=output_dir,
            output_filename=filename,
            use_sops=False,
        )
        created_files.append(f"{filename}.yaml")
    logger.info(
        f"Created namespace network policies for '{namespace}' "
        f"(owner deployment '{deployment['name']}'): {', '.join(created_files)}"
    )
    return created_files


def emit_letsencrypt_issuer(
    manifest_generator: ManifestGenerator,
    *,
    project_data: dict[str, Any],
    deployment_name: str,
    base_domain: str,
    issuer_config: str,
    cluster_name: str,
    namespace: str,
    output_dir: str,
) -> list[str]:
    """Write the Let's Encrypt Issuer plus its ACME solver policy; returns their filenames.

    Returns an empty list when neither the project nor the cluster has a contact email: ACME
    refuses to register an account without one. Whether ``issuer_config`` asks for a
    generated issuer at all is the caller's call, as the Helm paths and the component path
    accept a different set of values.
    """
    project_contact_email = project_data.get("config", {}).get("contact-email")
    contact_email = project_contact_email or get_letsencrypt_contact_email(cluster_name)
    if not contact_email:
        logger.warning(
            f"Cannot create Let's Encrypt Issuer for {base_domain}: no contact email configured "
            f"(set contact-email in project config or letsencrypt.contact_email in cluster config)"
        )
        return []

    manifest_filename = generate_issuer_manifest_name(base_domain, issuer_config, deployment_name).replace(".yaml", "")
    manifest_path = manifest_generator.create_manifest_file(
        template_path=os.path.join(MANIFEST_TEMPLATES_DIR, "issuer-letsencrypt.yaml.jinja"),
        values={
            "issuer_name": generate_issuer_name(base_domain, issuer_config, deployment_name),
            "issuer_secret_name": generate_issuer_secret_name(base_domain, issuer_config, deployment_name),
            "contact_email": contact_email,
            "staging": issuer_config == "letsencrypt-staging",
            "namespace": namespace,
        },
        output_dir=output_dir,
        output_filename=manifest_filename,
        use_sops=False,
    )
    logger.info(f"Created Let's Encrypt Issuer manifest for {base_domain}: {manifest_path}")

    return [
        f"{manifest_filename}.yaml",
        emit_acme_network_policy(
            manifest_generator,
            deployment_name=deployment_name,
            namespace=namespace,
            cluster_name=cluster_name,
            output_dir=output_dir,
        ),
    ]
