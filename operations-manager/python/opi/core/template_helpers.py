"""De helpers die de templateomgeving aan zijn sjablonen aanbiedt.

Filters (Nederlandse datums, een RRULE in woorden, de naam en de definitie achter een
services-regel), de paden waar sjablonen en statische bestanden staan, en de
inhoudsgehashte URL voor een eigen statisch bestand.

Dit bestand heette ``templates.py`` en zette daarnaast de roos-omgeving op. Die is met de
roos-bouwlijn verdwenen; wat overbleef is dit, en het wordt gebruikt door
``opi/core/templates_lotc.py``. Apart van die module omdat het geen omgeving is maar
gewone functies: de omgeving leunt via de dienstenregistry op de rest van de applicatie,
en deze helpers hoeven dat niet mee te slepen.
"""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opi.core.rrule_utils import format_rrule

if TYPE_CHECKING:
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

# De gangbare Nederlandse afkortingen. Niet af te leiden met [:3]: maart is "mrt" en niet
# "maa". Zie het short_month-argument hieronder.
DUTCH_MONTHS_SHORT = [
    "jan",
    "feb",
    "mrt",
    "apr",
    "mei",
    "jun",
    "jul",
    "aug",
    "sep",
    "okt",
    "nov",
    "dec",
]


def format_dutch_date(value: str | datetime | None, include_time: bool = True, short_month: bool = False) -> str:
    """
    Format a date/timestamp in Dutch format.

    Args:
        value: ISO timestamp string or datetime object
        include_time: Whether to include time in output
        short_month: Abbreviate the month to three letters ("17 sep 2026 23:18")

    Returns:
        Dutch formatted date string (e.g., "14 januari 2026 17:14")

    ``short_month`` bestaat voor een DICHTE, herhaalde context: de takentabel heeft zes
    kolommen waarvan er twee een tijdstip tonen, en "18 september 2026 01:40" is daar 174
    pixels breed tegen 126 afgekort - gemeten in de browser, zie
    tests/e2e/test_lotc_takentabel_datumkolom.py. Het is met opzet een optie op DIT filter
    en geen tweede formatteerfunctie: de omrekening naar Europe/Amsterdam en de Nederlandse
    maandnamen blijven op een plek, precies zoals ``include_time`` dat al doet. Wie een
    datum in gewone tekst toont, laat hem uitgeschreven.
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
        if short_month:
            month = DUTCH_MONTHS_SHORT[dt.month - 1]
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
TEMPLATES_DIR = OPI_DIR / "templates_lotc"
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

    De assets van het componentensysteem lopen hier NIET langs: die worden onder
    /static/lotc/ geserveerd door het pakket zelf, dus we bepalen die URL's niet.

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
