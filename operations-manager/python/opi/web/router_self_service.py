"""
Self-Service Portal route for the web interface.
"""

import json
import logging

from fastapi import HTTPException, Request

from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.cluster_config import CLUSTER_CONFIG
from opi.core.templates import get_templates
from opi.web.menu import get_menu_items

logger = logging.getLogger(__name__)


def get_cluster_options_for_template() -> list[dict]:
    """Get cluster options for the self-service portal dropdown.

    Returns:
        List of cluster option dicts with value and label.
    """
    cluster_labels = {
        "local": "Lokaal",
        "odcn-production": "Productie Cluster (ODC-Noord)",
    }

    options = [{"value": "", "label": "Selecteer een cluster"}]
    for cluster_name in CLUSTER_CONFIG:
        label = cluster_labels.get(cluster_name, cluster_name)
        options.append({"value": cluster_name, "label": label})

    return options


def get_cluster_base_domains_for_template() -> dict[str, list[dict]]:
    """Get base domain options per cluster for the self-service portal.

    Returns:
        Dict mapping cluster name to list of base domain options.
    """
    result = {}
    for cluster_name, config in CLUSTER_CONFIG.items():
        nice_url_config = config.get("nice_url", {})
        supported_domains = nice_url_config.get("supported_domains", [])

        domain_options = []
        for domain in supported_domains:
            # Add descriptive label
            if domain in ("kind", "local"):
                label = f"{domain} (lokaal)"
            else:
                label = domain
            domain_options.append({"value": domain, "label": label})

        result[cluster_name] = domain_options

    return result


@requires_sso
async def self_service_portal(request: Request):
    """
    Serve the Self-Service Portal form for creating new projects.

    This is a comprehensive form that allows users to:
    - Define project details (name, description, cluster)
    - Add team members with different roles
    - Select required services (web, SSO, storage, databases)

    The form uses ROOS components with a modern, user-friendly design
    and includes interactive features like user row cloning and
    service card selection.

    Returns:
        HTML response with the self-service portal form
    """
    try:
        templates = get_templates()
        user = get_current_user(request)

        # Get cluster configuration for the template
        cluster_options = get_cluster_options_for_template()
        cluster_base_domains = get_cluster_base_domains_for_template()

        return templates.TemplateResponse(
            "self-service-portal.html.j2",
            {
                "request": request,
                "title": "Nieuw Project - Self Service Portal",
                "menu_items": get_menu_items(user),
                "user": user,
                "cluster_options": cluster_options,
                "cluster_base_domains_json": json.dumps(cluster_base_domains),
            },
        )
    except Exception as e:
        import traceback

        error_details = traceback.format_exc()
        logger.error(f"Error serving Self-Service Portal form: {e!s}\n{error_details}")

        # Try to extract line number from Jinja2 error
        error_msg = str(e)
        if hasattr(e, "lineno"):
            error_msg = f"Line {e.lineno}: {error_msg}"

        # Include template source snippet if available
        if hasattr(e, "source") and hasattr(e, "lineno"):
            lines = e.source.splitlines()
            line_num = e.lineno - 1
            if 0 <= line_num < len(lines):
                error_msg += f"\nSource: {lines[line_num].strip()}"

        raise HTTPException(status_code=500, detail=f"Template error: {error_msg}")
