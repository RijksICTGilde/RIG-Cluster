"""Subdomain availability check and cluster domain helpers for the web interface."""

import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from opi.core.authz.actions import Action, action
from opi.core.cluster_config import CLUSTER_CONFIG
from opi.core.config import settings
from opi.http.auth.auth_decorators import get_current_user, requires_sso
from opi.http.rate_limiter import IPRateLimiter
from opi.services.catalog.publish_on_web.subdomain import (
    validate_base_domain,
    validate_subdomain_for_domain,
)
from opi.services.persistence.subdomain_registry import create_subdomain_connector

logger = logging.getLogger(__name__)

# Rate limited even though the endpoint is SSO-protected, with the same limits as the API one.
web_subdomain_check_rate_limiter = IPRateLimiter(requests_per_minute=30, burst=10)


def get_cluster_base_domains_for_template() -> dict[str, list[dict]]:
    """The base domain options per cluster, keyed by cluster name."""
    result = {}
    for cluster_name, config in CLUSTER_CONFIG.items():
        domains_config = config.get("domains", {})
        raw_domains = domains_config.get("supported_domains", [])

        domain_options = []
        for entry in raw_domains:
            domain = entry["domain"] if isinstance(entry, dict) else entry
            label = f"{domain} (lokaal)" if domain in ("kind", "local") else domain
            domain_options.append({"value": domain, "label": label})

        result[cluster_name] = domain_options

    return result


@action(Action.PORTAL_READ)
@requires_sso
async def check_subdomain_availability_web(request: Request) -> JSONResponse:
    """Is the ``subdomain`` free on ``base_domain``? SSO-protected and rate limited.

    Both come in as query parameters. The SSO requirement and the rate limit are what keep the
    subdomain registry out of reach of an enumeration attack.
    """
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

    safe_subdomain = subdomain[:64].replace("\n", "").replace("\r", "") if subdomain else ""
    safe_base_domain = base_domain[:64].replace("\n", "").replace("\r", "") if base_domain else ""
    logger.info(
        f"AUDIT: SSO subdomain check - subdomain={safe_subdomain}, base_domain={safe_base_domain}, user={user_email}"
    )

    try:
        is_valid, validation_error = validate_subdomain_for_domain(subdomain, base_domain, settings.CLUSTER_MANAGER)
        if not is_valid:
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
        logger.exception("Controleren van subdomein '%s' mislukt", subdomain)
        raise HTTPException(
            status_code=500,
            detail="Er kon niet worden gecontroleerd of dit subdomein vrij is. Probeer het over een minuut opnieuw.",
        ) from e


def _get_validation_error_code(error_message: str | None) -> str:
    """Map a validation error message, Dutch or English, to a code the frontend translates.

    A reserved subdomain gets the same ``SUBDOMAIN_NOT_AVAILABLE`` as one already in use, so
    the answer does not tell an attacker which of the two it is.
    """
    if not error_message:
        return "UNKNOWN_ERROR"

    error_lower = error_message.lower()

    # Order matters: the more specific patterns come first.
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
    # One answer for reserved and for already in use, so neither can be enumerated.
    if "niet beschikbaar" in error_lower or "not available" in error_lower:
        return "SUBDOMAIN_NOT_AVAILABLE"
    # An older wording the validators may still produce.
    if "gereserveerd" in error_lower or "reserved" in error_lower:
        return "SUBDOMAIN_NOT_AVAILABLE"

    return "SUBDOMAIN_INVALID"
