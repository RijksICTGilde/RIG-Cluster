"""
Template system configuration for Operations Manager.

This module sets up Jinja2 templates with ROOS components for the operations-manager UI.
Includes Babel i18n integration for multi-language support.
"""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import markupsafe
from fastapi.templating import Jinja2Templates
from jinja2 import FileSystemLoader
from jinja_roos_components import setup_components
from jinja_roos_components.extension import ComponentExtension

from opi.core.config import BUILD_DATE, VERSION
from opi.core.i18n import get_current_translation, get_requested_language
from opi.core.rrule_utils import format_rrule
from opi.core.version import get_version_info
from opi.services.registry import deployment_action_key

if TYPE_CHECKING:
    from starlette.requests import Request

    from opi.services.services import ServiceDefinition

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
                    # Check for timezone offset (+ or -) in the decimal part
                    for tz_char in ("+", "-"):
                        if tz_char in decimal_part:
                            idx = decimal_part.index(tz_char)
                            tz_part = decimal_part[idx:]
                            decimal_part = decimal_part[:idx]
                            break
                    value = parts[0] + "." + decimal_part[:6] + tz_part
            dt = datetime.fromisoformat(value)
        elif isinstance(value, datetime):
            dt = value
        else:
            return str(value)

        # Convert UTC to Amsterdam time for display
        from zoneinfo import ZoneInfo

        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo("Europe/Amsterdam"))

        day = dt.day
        month = DUTCH_MONTHS[dt.month - 1]
        year = dt.year

        if include_time:
            return f"{day} {month} {year} {dt.hour:02d}:{dt.minute:02d}"
        else:
            return f"{day} {month} {year}"

    except ValueError, TypeError, AttributeError:
        # Fallback: return truncated original
        return str(value)[:19] if value else "-"


def format_rrule_schedule(rrule: str | None) -> str:
    """Format an RRULE schedule string into a human-readable Dutch label.

    Example: "FREQ=DAILY;BYHOUR=2;BYMINUTE=0" -> "Dagelijks rond 02:00"

    Delegates to the shared format_rrule() utility. This wrapper preserves
    the original pass-through behavior for non-RRULE values (returns the
    raw string instead of "Geen").
    """
    if not rrule or not isinstance(rrule, str) or "FREQ=" not in rrule:
        return str(rrule or "")
    return format_rrule(rrule)


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
    from opi.services.services import service_entry_name

    return service_entry_name(service) or ""


def get_service_definition_for_entry(service: str | dict[str, Any]) -> ServiceDefinition | None:
    """The ServiceDefinition behind a services-list entry, or None for an unknown name.

    Lets a template show a service the way the wizard does (icon, description, help)
    from the raw project data, which only carries the service name. Accepts every entry
    format ``service_entry_name`` handles.
    """
    from opi.services.services import ServiceAdapter
    from opi.services.services_enums import ServiceType

    name = get_service_name(service)
    try:
        service_type = ServiceType(name)
    except ValueError:
        return None
    return ServiceAdapter.SERVICE_DEFINITIONS.get(service_type)


# Get the opi package directory (operations-manager/python/opi)
OPI_DIR = Path(__file__).parent.parent
TEMPLATES_DIR = OPI_DIR / "templates"
# Service-owned templates live next to their service under services/catalog/<svc>/ and
# are addressed as "<svc>/<file>". Putting the catalog dir on the search path lets a
# service deliver its own detail-page section (WP2) instead of the general template
# hardcoding an include (see UIEvent.PROJECT_SECTIONS / DetailPageSection).
CATALOG_DIR = OPI_DIR / "services" / "catalog"
# Own static files (operations-manager/python/static), mounted at /static.
STATIC_DIR = OPI_DIR.parent / "static"

# Content hash per static file, keyed by relative path, valued by ((mtime_ns, size), hash).
# The stat tuple is the invalidation key: one os.stat per render is negligible and it is
# exactly what makes this work in the skaffold loop - a synced file gets a new mtime, the
# hash is recomputed, the URL changes, no restart needed.
_STATIC_HASHES: dict[str, tuple[tuple[int, int], str]] = {}


def static_url(path: str) -> str:
    """URL for an own static file, with a content hash as ``?v=`` parameter.

    ``static_url("js/wizard.js")`` -> ``/static/js/wizard.js?v=1a2b3c4d``. Because the URL
    changes whenever the contents change, a browser can never serve a stale copy of a
    replaced file - it is a different URL. The ``?v=`` parameter is also what earns the
    long cache lifetime; see CacheControlledStaticFiles in opi/core/static_files.py.

    ROOS assets are deliberately NOT routed through here: they live under the separate
    /static/roos/dist mount and ROOS emits those URLs itself, so we do not control them.
    Adding a hash there is not possible without patching ROOS, and pinning them long-term
    without a hash would be wrong.

    A file that does not exist gets an unversioned URL, which falls back to no-cache
    rather than being pinned for a year.
    """
    relative_path = path.lstrip("/")
    file_path = STATIC_DIR / relative_path
    try:
        stat_result = file_path.stat()
    except OSError:
        return f"/static/{relative_path}"

    stat_key = (stat_result.st_mtime_ns, stat_result.st_size)
    cached = _STATIC_HASHES.get(relative_path)
    if cached is None or cached[0] != stat_key:
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()[:8]
        _STATIC_HASHES[relative_path] = (stat_key, digest)
    else:
        digest = cached[1]
    return f"/static/{relative_path}?v={digest}"


# Create templates instance
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# Add service-owned catalog templates to the SAME FileSystemLoader search path, so a
# service can deliver its own detail-page section. It must stay a FileSystemLoader:
# setup_components (below) registers the ROOS component templates by appending to
# loader.searchpath, which only a FileSystemLoader has.
if not isinstance(templates.env.loader, FileSystemLoader):
    raise TypeError("templates env loader must be a FileSystemLoader for catalog + ROOS search paths")
templates.env.loader.searchpath.append(str(CATALOG_DIR))

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
templates.env.globals["version"] = VERSION
templates.env.globals["build_date"] = BUILD_DATE
# Live version metadata (reads opi/version.json each call, so hot-synced dev builds
# are reflected without a restart). See opi/core/version.py.
templates.env.globals["version_info"] = get_version_info
# Content-hashed URLs for own static files; see static_url above.
templates.env.globals["static_url"] = static_url

# Register custom filters
templates.env.filters["service_name"] = get_service_name
templates.env.filters["service_definition"] = get_service_definition_for_entry
templates.env.filters["dutch_date"] = format_dutch_date
templates.env.filters["rrule_schedule"] = format_rrule_schedule
# The URL-safe id of a service-contributed deployment action; the confirmation dialog
# addresses an action by this key instead of by its endpoint (see registry).
templates.env.filters["deployment_action_key"] = deployment_action_key

# Register process_components filter for runtime-generated HTML that contains
# component tags (e.g. form_html from render_from_editables). The extension's
# preprocess only runs at template compile time, so runtime strings need this filter.
_component_ext = templates.env.extensions.get("jinja_roos_components.extension.ComponentExtension")
if not isinstance(_component_ext, ComponentExtension):
    raise TypeError("ComponentExtension not registered - setup_components must run first")


def _process_components(html: str) -> markupsafe.Markup:
    preprocessed = _component_ext.preprocess(html, name="process_components_filter", filename=None)
    rendered = templates.env.from_string(preprocessed).render()
    return markupsafe.Markup(rendered)  # noqa: S704


templates.env.filters["process_components"] = _process_components

# Enable i18n extension for {% trans %} blocks in templates
templates.env.add_extension("jinja2.ext.i18n")


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


def install_translations_for_request(request: Request) -> str:
    """Install Babel translations into the Jinja2 environment for the current request.

    Call this before rendering templates that use {% trans %} blocks.

    Returns:
        The resolved language code (e.g., "nl" or "en").
    """
    lang = get_requested_language(request)
    translation = get_current_translation(request)
    templates.env.install_gettext_translations(translation, newstyle=True)  # type: ignore[attr-defined]
    return lang
