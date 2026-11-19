"""Stateless manifest emitters shared by the Helm paths and the manifest generation."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from opi.core.cluster_config import (
    get_gateway_config,
    get_ingress_controller_selector,
    get_letsencrypt_contact_email,
    renders_gateway_api,
)
from opi.generation.manifests import MANIFEST_TEMPLATES_DIR
from opi.services.catalog.platform.network_policies import legacy_deployment_policy_filenames
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


def remove_namespace_network_policies(output_dir: str) -> None:
    """Remove a pre-RC-503 copy of the namespace-wide policies from a deployment directory.

    They are contributed to the project level now; this clears the copy of a project that
    has not been reprocessed since.
    """
    for filename in legacy_deployment_policy_filenames():
        stale = os.path.join(output_dir, filename)
        if os.path.exists(stale):
            os.remove(stale)
            logger.info(f"Removed namespace network policy '{filename}' from deployment directory {output_dir}")


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
            # Gateway routing: the HTTP-01 solver route hangs on the Gateway instead of on
            # a generated Ingress. Absent means the ingress solver, as before.
            "gateway": get_gateway_config(cluster_name) if renders_gateway_api(cluster_name) else None,
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
