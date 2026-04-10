"""
Subdomain availability check and cluster domain helpers for the web interface.
"""

import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from opi.api.router import IPRateLimiter
from opi.connectors.subdomain import (
    create_subdomain_connector,
    validate_base_domain,
    validate_subdomain,
)
from opi.core.auth_decorators import get_current_user, requires_sso
from opi.core.cluster_config import CLUSTER_CONFIG

logger = logging.getLogger(__name__)

# Rate limiter for web subdomain checks (same limits as API endpoint: 30 requests/minute per client)
# Even though SSO-protected, we still rate limit to prevent abuse by authenticated users
web_subdomain_check_rate_limiter = IPRateLimiter(requests_per_minute=30, burst=10)


def get_cluster_base_domains_for_template() -> dict[str, list[dict]]:
    """Get base domain options per cluster for the self-service portal.

    Returns:
        Dict mapping cluster name to list of base domain options.
    """
    result = {}
    for cluster_name, config in CLUSTER_CONFIG.items():
        nice_url_config = config.get("nice_url", {})
        raw_domains = nice_url_config.get("supported_domains", [])

        domain_options = []
        for entry in raw_domains:
            domain = entry["domain"] if isinstance(entry, dict) else entry
            # Add descriptive label
            label = f"{domain} (lokaal)" if domain in ("kind", "local") else domain
            domain_options.append({"value": domain, "label": label})

        result[cluster_name] = domain_options

    return result


@requires_sso
async def check_subdomain_availability_web(request: Request) -> JSONResponse:
    """
    SSO-protected endpoint to check subdomain availability.

    This endpoint requires the user to be authenticated via SSO, preventing
    unauthenticated enumeration attacks on the subdomain registry.

    Rate limited to prevent abuse even by authenticated users.

    Query Parameters:
        subdomain: The subdomain to check (e.g., "myapp")
        base_domain: The base domain (e.g., "rijks.app")

    Returns:
        JSON response with availability status and optional validation error code
    """
    # Rate limiting check - use robust client identification
    client_id = IPRateLimiter.get_client_identifier(request)
    if not web_subdomain_check_rate_limiter.is_allowed(client_id):
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please wait before checking again.",
        )

    subdomain = request.query_params.get("subdomain", "")
    base_domain = request.query_params.get("base_domain", "")

    user = get_current_user(request)
    user_email = user.get("email", "unknown") if user else "unknown"

    # Audit log
    safe_subdomain = subdomain[:64].replace("\n", "").replace("\r", "") if subdomain else ""
    safe_base_domain = base_domain[:64].replace("\n", "").replace("\r", "") if base_domain else ""
    logger.info(
        f"AUDIT: SSO subdomain check - subdomain={safe_subdomain}, base_domain={safe_base_domain}, user={user_email}"
    )

    try:
        # Validate subdomain format first
        is_valid, validation_error = validate_subdomain(subdomain)
        if not is_valid:
            # Map validation error to error code for frontend translation
            error_code = _get_validation_error_code(validation_error)
            return JSONResponse(
                {
                    "subdomain": subdomain.lower() if subdomain else "",
                    "base_domain": base_domain.lower() if base_domain else "",
                    "available": False,
                    "validation_error": validation_error,
                    "error_code": error_code,
                }
            )

        # Validate base_domain is supported
        is_valid_domain, domain_error = validate_base_domain(base_domain)
        if not is_valid_domain:
            return JSONResponse(
                {
                    "subdomain": subdomain.lower() if subdomain else "",
                    "base_domain": base_domain.lower() if base_domain else "",
                    "available": False,
                    "validation_error": domain_error,
                    "error_code": "INVALID_BASE_DOMAIN",
                }
            )

        connector = create_subdomain_connector()
        is_available = await connector.check_availability(subdomain, base_domain)

        return JSONResponse(
            {
                "subdomain": subdomain.lower(),
                "base_domain": base_domain.lower(),
                "available": is_available,
                "validation_error": None,
                "error_code": None if is_available else "SUBDOMAIN_TAKEN",
            }
        )
    except Exception as e:
        logger.error(f"Error checking subdomain availability: {e}")
        raise HTTPException(status_code=500, detail=f"Error checking subdomain availability: {e}")


def _get_validation_error_code(error_message: str | None) -> str:
    """Map validation error messages to error codes for frontend translation.

    This allows the frontend to use its own translations instead of relying
    on fragile string matching.

    NOTE: Reserved subdomains now return a generic "not available" message
    to prevent enumeration attacks. The error code SUBDOMAIN_NOT_AVAILABLE
    is used for both reserved and taken subdomains.

    Args:
        error_message: The validation error message (in Dutch or English)

    Returns:
        An error code string that the frontend can map to translations
    """
    if not error_message:
        return "UNKNOWN_ERROR"

    error_lower = error_message.lower()

    # Check for common error patterns (order matters - check more specific patterns first)
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
    if "ongeldige tekens" in error_lower or "invalid character" in error_lower:
        return "SUBDOMAIN_INVALID_CHARS"
    # Generic "not available" for both reserved and taken subdomains (security: no enumeration)
    if "niet beschikbaar" in error_lower or "not available" in error_lower:
        return "SUBDOMAIN_NOT_AVAILABLE"
    # Legacy patterns (kept for backwards compatibility)
    if "gereserveerd" in error_lower or "reserved" in error_lower:
        return "SUBDOMAIN_NOT_AVAILABLE"  # Changed from SUBDOMAIN_RESERVED

    return "SUBDOMAIN_INVALID"
