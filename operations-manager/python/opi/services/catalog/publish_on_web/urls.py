"""The public URLs a project exposes, per deployment and component.

Publish-on-web is the service that decides whether a component is reachable from the
internet and under which hostname, so the derivation lives here rather than in the page
that happens to show it first.

Two readers today: the project detail page lists these as links, and the invite form
offers them as the destination of its success button. That second one is why this moved
out of ``web/router.py`` -- a user setting up an invitation knows which deployment and
component they want people to land on, not the URL, so the form has to derive it for
them.
"""

from __future__ import annotations

import logging
from typing import Any

from opi.core.cluster_config import get_ingress_postfix, get_ingress_tls_enabled
from opi.services.catalog.publish_on_web.domain_config import DomainSetting, get_domain_setting
from opi.utils.naming import HostnameFormat, generate_public_url, get_component_ingress_map

logger = logging.getLogger(__name__)


def public_urls_for_deployment(
    project_data: dict[str, Any],
    deployment: dict[str, Any],
    project_name: str,
    project_file_handler: Any,
) -> list[dict[str, str]]:
    """Every public URL this deployment exposes, one entry per component/path pair.

    Returns dicts with ``component_name``, ``ingress_name``, ``hostname``, ``path`` and
    ``url``. Components without publish-on-web contribute nothing, which is the point:
    they have no public address to offer.

    A deployment on an unknown cluster, or one whose hostname cannot be derived, yields
    an empty list rather than raising: this feeds a page and a picker, and neither should
    break because one deployment is mid-configuration.
    """
    cluster = deployment.get("cluster")
    if not cluster:
        return []

    try:
        ingress_postfix = get_ingress_postfix(cluster)
        use_https = get_ingress_tls_enabled(cluster)
        hostname_format = HostnameFormat.from_domain_mode(get_domain_setting(deployment, DomainSetting.DOMAIN_MODE))

        links: list[dict[str, str]] = []
        for component in deployment.get("components", []) or []:
            component_name = component.get("reference")
            if not component_name:
                continue
            if not project_file_handler.extract_component_publish_on_web(project_data, component_name):
                continue

            ingress_map = get_component_ingress_map(
                component_name=component_name,
                deployment_name=deployment["name"],
                project_name=project_name,
                ingress_postfix=ingress_postfix,
                subdomain=get_domain_setting(deployment, DomainSetting.SUBDOMAIN),
                base_domain=get_domain_setting(deployment, DomainSetting.BASE_DOMAIN),
                hostname_format=hostname_format,
                domain_format=get_domain_setting(deployment, DomainSetting.DOMAIN_FORMAT),
                project_data=project_data,
                cluster=cluster,
            )
            paths = project_file_handler.extract_deployment_component_paths(project_data, deployment, component_name)
            for ingress_name, hostname in ingress_map.items():
                for path_config in paths:
                    match = path_config.get("match") or "/"
                    links.append(
                        {
                            "component_name": component_name,
                            "ingress_name": ingress_name,
                            "hostname": hostname,
                            "path": match,
                            "url": generate_public_url(hostname, use_https, match),
                        }
                    )
        return links
    except (KeyError, ValueError, AttributeError) as exc:
        logger.warning(
            "Could not derive public URLs for deployment %s of %s: %s", deployment.get("name"), project_name, exc
        )
        return []


def public_url_map_for_deployment(
    project_data: dict[str, Any],
    deployment: dict[str, Any],
    project_name: str,
    project_file_handler: Any,
) -> dict[str, str]:
    """Component name -> its primary public URL, for this deployment.

    The same derivation as ``public_urls_for_deployment``, folded to one address per
    component and without the path: what a caller means by "the address of this
    component". A component that gets no ingress is absent from the map rather than
    present with a guessed hostname -- an address that answers 404 is worse than no
    address, because a client stores it and passes it on.

    This is the one source for that answer. The refresh task reports it, the deployment
    endpoint returns it and the detail page links it; deriving it three times is how they
    came to disagree (RC-104, vraag 13).
    """
    cluster = deployment.get("cluster")
    if not cluster:
        return {}

    try:
        use_https = get_ingress_tls_enabled(cluster)
    except (KeyError, ValueError) as exc:
        logger.warning("Could not resolve ingress config for cluster '%s': %s", cluster, exc)
        return {}

    urls: dict[str, str] = {}
    for link in public_urls_for_deployment(project_data, deployment, project_name, project_file_handler):
        urls.setdefault(link["component_name"], generate_public_url(link["hostname"], use_https))
    return urls


def public_urls_for_project(project_data: dict[str, Any], project_file_handler: Any) -> list[dict[str, str]]:
    """Every public URL in the project, with the deployment name added to each entry.

    Used to offer a destination to pick. The deployment name is part of the entry because
    the same component in two deployments has two addresses, and "frontend" alone would
    not tell them apart.
    """
    project_name = project_data.get("name", "")
    urls: list[dict[str, str]] = []
    for deployment in project_data.get("deployments", []) or []:
        if not isinstance(deployment, dict):
            continue
        urls.extend(
            {**link, "deployment_name": deployment.get("name", "")}
            for link in public_urls_for_deployment(project_data, deployment, project_name, project_file_handler)
        )
    return urls
