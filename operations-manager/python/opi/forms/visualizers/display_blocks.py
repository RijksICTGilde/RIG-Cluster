"""Display block compute functions for server-rendered display areas."""

from __future__ import annotations

from typing import Any

from opi.utils.naming import DOMAIN_FORMAT_TEMPLATES, generate_hostname_from_format


def compute_url_preview(yaml_data: dict[str, Any]) -> dict[str, Any]:
    """Compute URL preview context from wizard form data.

    Generates example URLs for each component based on the selected
    domain-format, subdomain, base-domain, and deployment name.

    Returns a template context with ``urls`` (list of dicts with
    ``component`` and ``url`` keys) and ``has_urls`` boolean.
    """
    deployments = yaml_data.get("deployments", [])
    if not deployments or not isinstance(deployments[0], dict):
        return {"urls": [], "has_urls": False}

    dep = deployments[0]
    domain_format = dep.get("domain-format")
    if not domain_format or domain_format not in DOMAIN_FORMAT_TEMPLATES:
        return {"urls": [], "has_urls": False}

    deployment_name = dep.get("name", "deployment")
    subdomain = dep.get("subdomain") or ""
    base_domain = dep.get("base-domain") or ""
    custom_domain = dep.get("base-domain:custom") or ""

    # Resolve the domain: use custom if sentinel, else base_domain, else placeholder
    domain = custom_domain or "voorbeeld.nl" if base_domain == "__custom__" else base_domain or "domein.nl"

    project_name = yaml_data.get("display-name", "project") or "project"

    # Get component names from yaml_data
    components = yaml_data.get("components", [])
    component_names = [comp["name"] for comp in components if isinstance(comp, dict) and comp.get("name")]

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

    return {"urls": urls, "has_urls": bool(urls)}
