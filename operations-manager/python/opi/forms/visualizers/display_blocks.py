"""Display block compute functions for server-rendered display areas."""

from __future__ import annotations

from typing import Any

from opi.core import config as opi_config
from opi.core.cluster_config import CLUSTER_CONFIG
from opi.services.catalog.publish_on_web.domain_config import (
    DOMAIN_FORMAT_TEMPLATES,
    DomainSetting,
    get_domain_setting,
    subdomain_used_by_format,
)
from opi.services.catalog.publish_on_web.hostnames import generate_hostname_from_format
from opi.services.catalog.publish_on_web.subdomain import is_deployment_domain_approved


def _get_default_domain() -> str:
    """Return the ingress postfix domain for the current cluster (the cluster default)."""
    cluster = opi_config.settings.CLUSTER_MANAGER
    if cluster and cluster in CLUSTER_CONFIG:
        postfix = CLUSTER_CONFIG[cluster].get("ingress_postfix", "")
        if postfix:
            return postfix.lstrip(".")
    return "domein.nl"


def compute_url_preview(yaml_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """An example URL per component, from the chosen domain-format, subdomain and domain.

    ``context`` is the DisplayBlock context, and ``deployment_index`` in it says which
    deployment to preview. The template context carries ``urls``, with a ``component`` and a
    ``url`` per entry, plus ``has_urls``.
    """
    deployment_index = context.get("deployment_index", 0)
    deployments = yaml_data.get("deployments", [])
    if len(deployments) <= deployment_index or not isinstance(deployments[deployment_index], dict):
        return {"urls": [], "has_urls": False}

    dep = deployments[deployment_index]
    domain_format = get_domain_setting(dep, DomainSetting.DOMAIN_FORMAT) or "component-deployment-project"
    if domain_format not in DOMAIN_FORMAT_TEMPLATES:
        return {"urls": [], "has_urls": False}

    deployment_name = dep.get("name", "deployment")
    subdomain = get_domain_setting(dep, DomainSetting.SUBDOMAIN) or ""
    base_domain = get_domain_setting(dep, DomainSetting.BASE_DOMAIN) or ""
    custom_domain = dep.get("base-domain:custom") or ""

    # An explicit if/else, because chaining `or` with a ternary here reads the wrong way.
    if base_domain == "__custom__":  # noqa: SIM108
        domain = custom_domain or "voorbeeld.nl"
    else:
        domain = base_domain or _get_default_domain()

    project_name = yaml_data.get("name") or "projectid"

    # The component names scoped to this deployment. While editing, that is the deployment's
    # own components carrying publish-on-web; while creating, there are no deployment
    # components and no services yet, so every project-level component name is previewed.
    dep_components = dep.get("components", [])
    dep_component_refs = {c["reference"] for c in dep_components if isinstance(c, dict) and c.get("reference")}

    all_components = yaml_data.get("components", [])

    any_services_configured = any(
        (isinstance(c, dict) and (c.get("services") or c.get("uses-services"))) for c in all_components
    )

    component_names: list[str] = []
    for comp in all_components:
        if not isinstance(comp, dict) or not comp.get("name"):
            continue
        name = comp["name"]
        if dep_component_refs and name not in dep_component_refs:
            continue
        if any_services_configured:
            from opi.services.services import service_entry_name
            from opi.services.services_enums import ServiceType

            comp_services = comp.get("services", []) + comp.get("uses-services", [])
            # Format-agnostic: ``s.keys()`` on a record yields name/reference/config, so a
            # component whose publish-on-web carries config would drop out of the preview.
            service_names = [svc_name for s in comp_services if (svc_name := service_entry_name(s)) is not None]
            if ServiceType.PUBLISH_ON_WEB.value not in service_names:
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

    root_component = get_domain_setting(dep, DomainSetting.ROOT_COMPONENT)
    if root_component and has_component_var and "." in domain_format:
        short_template = template.replace("{component}.", "")
        short_url = short_template.format(
            deployment=deployment_name.lower(),
            project=project_name.lower(),
            subdomain=(subdomain or "subdomein").lower(),
            domain=domain,
        )
        urls.append({"component": f"{root_component} (root)", "url": short_url})

    bare_domain_component = get_domain_setting(dep, DomainSetting.BARE_DOMAIN_COMPONENT)
    if bare_domain_component and domain:
        urls.append({"component": f"{bare_domain_component} (kaal domein)", "url": domain})

    needs_approval = False
    cluster = opi_config.settings.CLUSTER_MANAGER
    effective_domain = domain if base_domain != "__custom__" else custom_domain
    if effective_domain and cluster:
        used_subdomain = subdomain_used_by_format(subdomain or None, domain_format)
        needs_approval = not is_deployment_domain_approved(yaml_data, effective_domain, used_subdomain, cluster)

    return {"urls": urls, "has_urls": bool(urls), "needs_approval": needs_approval}
