"""The helpers the template environment offers its templates.

Filters (Dutch dates, an RRULE in words, the name and the definition behind a services
entry), the paths where templates and static files live, and the content-hashed URL for an
own static file.

Used by ``opi/http/render/templates_lotc.py``, and apart from that module because these are plain
functions: the environment leans on the rest of the application through the service
registry, and these helpers need not drag that along.
"""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opi.core.rrule_utils import format_rrule

if TYPE_CHECKING:
    from opi.services.services import ServiceDefinition

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

# The common Dutch abbreviations. Not derivable with [:3]: maart is "mrt" and not "maa".
# See the short_month argument below.
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


def format_dutch_date(
    value: str | datetime | None, include_time: bool = True, short_month: bool = False, only_time: bool = False
) -> str:
    """A date or timestamp in Dutch form, such as "14 januari 2026 17:14".

    ``short_month`` gives "17 sep 2026 23:18" and ``only_time`` gives "23:18", for something
    that expires today.

    ``short_month`` is for a DENSE, repeated context, such as the two time columns of the
    task table (tests/e2e/test_lotc_takentabel_datumkolom.py). A date in running text stays
    written out.
    """
    if not value:
        return "-"

    try:
        if isinstance(value, str):
            # An ISO timestamp such as "2026-01-14T17:14:34.335860214Z", in any of its forms.
            value = value.replace("Z", "+00:00")
            if "." in value:
                # fromisoformat takes six decimals at most, and kubectl hands out nine.
                parts = value.split(".")
                if len(parts[1]) > 6:
                    tz_part = ""
                    decimal_part = parts[1]
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

        from zoneinfo import ZoneInfo

        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo("Europe/Amsterdam"))

        day = dt.day
        month = (DUTCH_MONTHS_SHORT if short_month else DUTCH_MONTHS)[dt.month - 1]
        year = dt.year

        if only_time:
            return f"{dt.hour:02d}:{dt.minute:02d}"
        if include_time:
            return f"{day} {month} {year} {dt.hour:02d}:{dt.minute:02d}"
        else:
            return f"{day} {month} {year}"

    except ValueError, TypeError, AttributeError:
        return str(value)[:19] if value else "-"


def shorten_image_digest(image: str | None) -> str:
    """An image reference with the digest shortened to twelve characters.

    ``ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344a1b2c3d4...`` becomes
    ``ghcr.io/minbzk/moza-profiel-service@sha256:25ab6344a1b2``. A reference with a tag
    instead of a digest stays whole: there is nothing to shorten there.

    The full 64 characters do not fit on a line and add nothing: whoever compares versions
    has enough with the start, and whoever needs the full digest looks in the project file.

    A filter and not a ``[:19]`` in the template: a ``[:N]`` there trips the allowlist of
    tests/test_dates_go_through_one_filter.py, which stops truncations because they are
    nearly always a timestamp that should go through ``dutch_date``.
    """
    repository, separator, digest = (image or "").partition("@")
    if not separator:
        return image or ""
    algorithm, colon, hex_part = digest.partition(":")
    if not colon:
        return repository + separator + digest[:12]
    return f"{repository}@{algorithm}:{hex_part[:12]}"


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
    """The service name out of either entry form.

    A services entry is either a string (``"publish-on-web"``) or a dict with the service
    name as its key (``{"keycloak": {"config": {...}}}``).
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


OPI_DIR = Path(__file__).parent.parent.parent
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
    replaced file: it is a different URL. The ``?v=`` parameter is also what earns the
    long cache lifetime; see CacheControlledStaticFiles in opi/http/render/static_files.py.

    The assets of the component system do NOT pass through here: those are served under
    /static/lotc/ by the package itself, so we do not decide those URLs.

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


def format_step_duration(seconds: float | None) -> str:
    """The duration of a progress step as readable text, with the comma as separator.

    Below a minute there is one decimal, because the difference between 0,4 and 4 seconds is
    exactly what gives away a step that takes too long. Above it, minutes.
    """
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{seconds:.1f}".replace(".", ",") + " s"
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes} min {rest} s"


# The label and the colour of the cluster in the header bar, so you see which environment
# you work in. Staging stands out; production stays calm, because a bar that always raises
# an alarm is one you stop seeing after a day.
CLUSTER_TAGS: dict[str, tuple[str, str]] = {
    "local": ("Local", "accent"),
    "sandboxed-local": ("Sandbox", "accent"),
    "odcn-staging": ("ODCN Staging", "warning"),
    "odcn-production": ("ODCN Production", "neutral"),
}


def cluster_tag(cluster_manager: str) -> tuple[str, str]:
    """Label and ``nldd-tag`` colour for a cluster. An unknown cluster shows its name."""
    if cluster_manager.startswith("sandboxed-local"):
        return CLUSTER_TAGS["sandboxed-local"]
    return CLUSTER_TAGS.get(cluster_manager, (cluster_manager, "neutral"))
