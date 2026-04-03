"""Display block compute functions for server-rendered display areas."""

from __future__ import annotations

from typing import Any

from opi.connectors.subdomain import is_deployment_domain_approved
from opi.core import config as opi_config
from opi.core.cluster_config import CLUSTER_CONFIG
from opi.utils.naming import DOMAIN_FORMAT_TEMPLATES, generate_hostname_from_format


def _get_default_domain() -> str:
    """Return the ingress postfix domain for the current cluster (the cluster default)."""
    cluster = opi_config.settings.CLUSTER_MANAGER
    if cluster and cluster in CLUSTER_CONFIG:
        postfix = CLUSTER_CONFIG[cluster].get("ingress_postfix", "")
        if postfix:
            return postfix.lstrip(".")
    return "domein.nl"


def compute_url_preview(yaml_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Compute URL preview context from wizard form data.

    Generates example URLs for each component based on the selected
    domain-format, subdomain, base-domain, and deployment name.

    Args:
        yaml_data: The full form data dict.
        context: DisplayBlock context; reads ``deployment_index`` (default 0).

    Returns a template context with ``urls`` (list of dicts with
    ``component`` and ``url`` keys) and ``has_urls`` boolean.
    """
    deployment_index = context.get("deployment_index", 0)
    deployments = yaml_data.get("deployments", [])
    if len(deployments) <= deployment_index or not isinstance(deployments[deployment_index], dict):
        return {"urls": [], "has_urls": False}

    dep = deployments[deployment_index]
    domain_format = dep.get("domain-format") or "component-deployment-project"
    if domain_format not in DOMAIN_FORMAT_TEMPLATES:
        return {"urls": [], "has_urls": False}

    deployment_name = dep.get("name", "deployment")
    subdomain = dep.get("subdomain") or ""
    base_domain = dep.get("base-domain") or ""
    custom_domain = dep.get("base-domain:custom") or ""

    # Resolve the domain: custom when sentinel, selected value, or cluster default.
    # Explicit if/else avoids the operator-precedence trap of chained `or`/ternary.
    if base_domain == "__custom__":  # noqa: SIM108
        domain = custom_domain or "voorbeeld.nl"
    else:
        domain = base_domain or _get_default_domain()

    project_name = yaml_data.get("name") or "projectid"

    # Get component names scoped to this deployment.
    # In edit mode (deployment has components list), filter to components
    # assigned to this deployment that have the publish-on-web service.
    # In create mode (no deployment components yet, no services configured),
    # show all project-level component names as a preview.
    dep_components = dep.get("components", [])
    dep_component_refs = {c["reference"] for c in dep_components if isinstance(c, dict) and c.get("reference")}

    all_components = yaml_data.get("components", [])

    # Detect whether any component has services configured at all.
    # During the create wizard, services haven't been assigned yet.
    any_services_configured = any(
        (isinstance(c, dict) and (c.get("services") or c.get("uses-services"))) for c in all_components
    )

    component_names: list[str] = []
    for comp in all_components:
        if not isinstance(comp, dict) or not comp.get("name"):
            continue
        name = comp["name"]
        # Filter to deployment's components (skip filter if deployment has no component list yet)
        if dep_component_refs and name not in dep_component_refs:
            continue
        # Filter to components with publish-on-web service (skip filter during create wizard)
        if any_services_configured:
            comp_services = comp.get("services", []) + comp.get("uses-services", [])
            service_names = []
            for s in comp_services:
                if isinstance(s, str):
                    service_names.append(s)
                elif isinstance(s, dict):
                    service_names.extend(s.keys())
            if "publish-on-web" not in service_names:
                continue
        component_names.append(name)

    if not component_names:
        component_names = ["component"]

    template = DOMAIN_FORMAT_TEMPLATES[domain_format]
    has_component_var = "{component}" in template

    urls: list[dict[str, str]] = []
    if has_component_var:
        for name in component_names:
            url = generate_hostname_from_format(
                domain_format=domain_format,
                component_name=name,
                deployment_name=deployment_name,
                project_name=project_name,
                subdomain=subdomain or "subdomein",
                domain=domain,
            )
            urls.append({"component": name, "url": url})
    else:
        url = generate_hostname_from_format(
            domain_format=domain_format,
            component_name="",
            deployment_name=deployment_name,
            project_name=project_name,
            subdomain=subdomain or "subdomein",
            domain=domain,
        )
        urls.append({"component": "(gedeeld)", "url": url})

    # Root component short URL
    root_component = dep.get("root-component")
    if root_component and has_component_var and "." in domain_format:
        # Generate the short URL (without component prefix)
        short_template = template.replace("{component}.", "")
        short_url = short_template.format(
            deployment=deployment_name.lower(),
            project=project_name.lower(),
            subdomain=(subdomain or "subdomein").lower(),
            domain=domain,
        )
        urls.append({"component": f"{root_component} (root)", "url": short_url})

    # Bare domain URL when a component is selected for expose-on-bare-domain
    bare_domain_component = dep.get("expose-component-on-bare-domain")
    if bare_domain_component and domain:
        urls.append({"component": f"{bare_domain_component} (kaal domein)", "url": domain})

    # Check if domain or subdomain needs approval
    needs_approval = False
    cluster = opi_config.settings.CLUSTER_MANAGER
    effective_domain = domain if base_domain != "__custom__" else custom_domain
    if effective_domain and cluster:
        needs_approval = not is_deployment_domain_approved(yaml_data, effective_domain, subdomain or None, cluster)

    return {"urls": urls, "has_urls": bool(urls), "needs_approval": needs_approval}
