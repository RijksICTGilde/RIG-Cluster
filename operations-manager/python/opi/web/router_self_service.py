"""
Self-Service Portal route for the web interface.
"""

import json
import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from opi.connectors.subdomain import (
    create_subdomain_connector,
    validate_base_domain,
    validate_subdomain,
)
from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.cluster_config import CLUSTER_CONFIG
from opi.core.templates import get_templates
from opi.utils.csrf import ensure_csrf_token
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

        # Generate CSRF token for form protection
        csrf_token = ensure_csrf_token(request)

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
                "csrf_token": csrf_token,
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


@requires_sso
async def check_subdomain_availability_web(request: Request) -> JSONResponse:
    """
    SSO-protected endpoint to check subdomain availability.

    This endpoint requires the user to be authenticated via SSO, preventing
    unauthenticated enumeration attacks on the subdomain registry.

    Query Parameters:
        subdomain: The subdomain to check (e.g., "myapp")
        base_domain: The base domain (e.g., "rijks.app")

    Returns:
        JSON response with availability status and optional validation error code
    """
    subdomain = request.query_params.get("subdomain", "")
    base_domain = request.query_params.get("base_domain", "")

    user = get_current_user(request)
    user_email = user.get("email", "unknown") if user else "unknown"

    # Audit log
    safe_subdomain = subdomain[:64].replace("\n", "").replace("\r", "") if subdomain else ""
    safe_base_domain = base_domain[:64].replace("\n", "").replace("\r", "") if base_domain else ""
    logger.info(
        f"AUDIT: SSO subdomain check - subdomain={safe_subdomain}, "
        f"base_domain={safe_base_domain}, user={user_email}"
    )

    try:
        # Validate subdomain format first
        is_valid, validation_error = validate_subdomain(subdomain)
        if not is_valid:
            # Map validation error to error code for frontend translation
            error_code = _get_validation_error_code(validation_error)
            return JSONResponse({
                "subdomain": subdomain.lower() if subdomain else "",
                "base_domain": base_domain.lower() if base_domain else "",
                "available": False,
                "validation_error": validation_error,
                "error_code": error_code,
            })

        # Validate base_domain is supported
        is_valid_domain, domain_error = validate_base_domain(base_domain)
        if not is_valid_domain:
            return JSONResponse({
                "subdomain": subdomain.lower() if subdomain else "",
                "base_domain": base_domain.lower() if base_domain else "",
                "available": False,
                "validation_error": domain_error,
                "error_code": "INVALID_BASE_DOMAIN",
            })

        connector = create_subdomain_connector()
        is_available = await connector.check_availability(subdomain, base_domain)

        return JSONResponse({
            "subdomain": subdomain.lower(),
            "base_domain": base_domain.lower(),
            "available": is_available,
            "validation_error": None,
            "error_code": None if is_available else "SUBDOMAIN_TAKEN",
        })
    except Exception as e:
        logger.error(f"Error checking subdomain availability: {e}")
        raise HTTPException(status_code=500, detail=f"Error checking subdomain availability: {e}")


def _get_validation_error_code(error_message: str | None) -> str:
    """Map validation error messages to error codes for frontend translation.

    This allows the frontend to use its own translations instead of relying
    on fragile string matching.

    Args:
        error_message: The validation error message (in Dutch or English)

    Returns:
        An error code string that the frontend can map to translations
    """
    if not error_message:
        return "UNKNOWN_ERROR"

    error_lower = error_message.lower()

    # Check for common error patterns
    if "leeg" in error_lower or "empty" in error_lower:
        return "SUBDOMAIN_EMPTY"
    if "kort" in error_lower or "short" in error_lower or "minimaal" in error_lower:
        return "SUBDOMAIN_TOO_SHORT"
    if "lang" in error_lower or "long" in error_lower or "maximaal" in error_lower:
        return "SUBDOMAIN_TOO_LONG"
    if "beginnen" in error_lower or "start with" in error_lower or "begin" in error_lower:
        return "SUBDOMAIN_INVALID_START"
    if "eindigen" in error_lower or "end with" in error_lower:
        return "SUBDOMAIN_INVALID_END"
    if "kleine letters" in error_lower or "lowercase" in error_lower:
        return "SUBDOMAIN_INVALID_CHARS"
    if "gereserveerd" in error_lower or "reserved" in error_lower:
        return "SUBDOMAIN_RESERVED"
    if "ongeldige tekens" in error_lower or "invalid character" in error_lower:
        return "SUBDOMAIN_INVALID_CHARS"

    return "SUBDOMAIN_INVALID"
