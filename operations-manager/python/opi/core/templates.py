"""
Template system configuration for Operations Manager.

This module sets up Jinja2 templates with ROOS components for the operations-manager UI.
"""

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi.templating import Jinja2Templates
from jinja_roos_components import setup_components

# Dutch month names
DUTCH_MONTHS = [
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
]


def format_dutch_date(value: str | datetime | None, include_time: bool = True) -> str:
    """
    Format a date/timestamp in Dutch format.

    Args:
        value: ISO timestamp string or datetime object
        include_time: Whether to include time in output

    Returns:
        Dutch formatted date string (e.g., "14 januari 2026 17:14")
    """
    if not value:
        return "-"

    try:
        if isinstance(value, str):
            # Parse ISO format timestamp (e.g., "2026-01-14T17:14:34.335860214Z")
            # Handle various ISO formats
            value = value.replace("Z", "+00:00")
            if "." in value:
                # Truncate nanoseconds to microseconds (max 6 digits after decimal)
                parts = value.split(".")
                if len(parts[1]) > 6:
                    # Keep timezone info if present
                    tz_part = ""
                    decimal_part = parts[1]
                    if "+" in decimal_part:
                        idx = decimal_part.index("+")
                        tz_part = decimal_part[idx:]
                        decimal_part = decimal_part[:idx]
                    value = parts[0] + "." + decimal_part[:6] + tz_part
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif isinstance(value, datetime):
            dt = value
        else:
            return str(value)

        day = dt.day
        month = DUTCH_MONTHS[dt.month - 1]
        year = dt.year

        if include_time:
            return f"{day} {month} {year} {dt.hour:02d}:{dt.minute:02d}"
        else:
            return f"{day} {month} {year}"

    except (ValueError, TypeError, AttributeError):
        # Fallback: return truncated original
        return str(value)[:19] if value else "-"


def get_service_name(service: str | dict[str, Any]) -> str:
    """
    Extract service name from mixed service format.

    Services can be either:
    - A string: "publish-on-web"
    - A dict with service name as key: {"keycloak": {"config": {...}}}

    Args:
        service: Service definition (string or dict)

    Returns:
        Service name as string
    """
    if isinstance(service, str):
        return service
    if isinstance(service, dict):
        # Return the first key as the service name
        return next(iter(service.keys()), "")
    return str(service)


# Get the opi package directory (operations-manager/python/opi)
OPI_DIR = Path(__file__).parent.parent
TEMPLATES_DIR = OPI_DIR / "templates"

# Create templates instance
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Setup ROOS components immediately on the global templates instance
# Enable strict validation in development/debug mode

# strict_mode = os.getenv("DEBUG", "false").lower() == "true" or os.getenv("ENVIRONMENT", "development") == "development"
strict_mode = True  # always!
setup_components(
    templates.env,
    htmx=True,
    static_url_prefix="/static/roos/",
    user_css_files=["/static/operations.css"],
    strict_validation=strict_mode,
)

# Add global variables that components might need
templates.env.globals["roos_assets_base_url"] = "/static/roos/dist/"

# Register custom filters
templates.env.filters["service_name"] = get_service_name
templates.env.filters["dutch_date"] = format_dutch_date


def setup_templates() -> Jinja2Templates:
    """
    Get the configured templates instance.

    Note: Setup is already done during module initialization.

    Returns:
        Configured Jinja2Templates instance with ROOS components
    """
    return templates


def get_templates() -> Jinja2Templates:
    """
    Get configured templates instance.

    Returns:
        Jinja2Templates instance with ROOS components
    """
    return templates
