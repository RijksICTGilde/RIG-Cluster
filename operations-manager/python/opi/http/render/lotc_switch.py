"""How a route renders its page, and what the redrawn pages ask for in the way of data.

What is left here is the boring part: putting a route's data into the shape the redrawn
page reads, plus the light/dark scheme choice at the bottom.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode, urlparse
from zoneinfo import ZoneInfo

from sqlalchemy.exc import SQLAlchemyError

from opi.core.config import settings
from opi.services.audit import LIST_DEFAULT_LIMIT, LIST_MAX_LIMIT, AuditCursor, list_actions
from opi.services.step_up import step_up_until
from opi.services.user_service import get_user_service

if TYPE_CHECKING:
    from fastapi import Request
    from starlette.responses import Response

logger = logging.getLogger(__name__)


def render(request: Request, *, template: str, context: dict[str, Any], status_code: int = 200) -> Response:
    """Render ``context`` with ``template`` from ``opi/templates_lotc/``.

    Args:
        request: the incoming request.
        template: the template, in ``opi/templates_lotc/``.
        context: the data.
        status_code: the HTTP status, for a page that IS a refusal (429).

    The import sits inside and not at the top to avoid a cycle: the template environment
    leans on the forms layer, which is imported by the routes that use this module.
    """
    from opi.http.render.templates_lotc import templates_lotc

    # For the account menu: a page without a session (an error page) has no step-up.
    raised_until = step_up_until(request) if "session" in request.scope else None
    email = str((getattr(request.state, "user", None) or {}).get("email", ""))
    may_raise = get_user_service().is_platform_admin(email)
    return templates_lotc.TemplateResponse(
        request,
        template,
        {"verhoogd_tot": raised_until, "kan_verhogen": may_raise, **context},
        status_code=status_code,
    )


def render_fragment(request: Request, *, template: str, context: dict[str, Any]) -> str:
    """Render a FRAGMENT as an HTML string.

    The counterpart of :func:`render` for pieces that do not become a ``TemplateResponse``
    but a string the route itself wraps in an ``HTMLResponse``.

    No second pass ever goes over it. A second Jinja render would execute the filled-in
    values as a template, which has leaked in this codebase before.

    ``request`` supplies the CSP nonce; a TemplateResponse gets it from the
    context_processor, a loose render does not.
    """
    from opi.http.render.templates_lotc import csp_nonce, templates_lotc

    return templates_lotc.env.get_template(template).render({**context, "csp_nonce": csp_nonce(request)})


def build_lotc_services(
    services_info: list[dict[str, Any]],
    user: dict[str, Any] | None,
) -> dict[str, Any]:
    """Turn the real service data into what the redrawn services page expects."""
    from opi.http.render.navigation_lotc import get_navigation
    from opi.utils.nldd_iconen import to_nldd_icon

    # Every service the route supplies lands on the page, including one with
    # ``hidden=True``: that flag means "do not offer in the WIZARD", and the overview page
    # does show them because they supply environment variables you have to be able to
    # look up.
    services: list[dict[str, Any]] = [
        {
            "name": entry["service"].value,
            "label": definition.name,
            "summary": definition.description,
            "icon": to_nldd_icon(definition.icon),
            "color": definition.color,
            "help_template": getattr(definition, "help_template", None),
            # The deprecation marker from the caller (services_router computes it against
            # the managing cluster); None where the service is current.
            "replaced_by": entry.get("replaced_by"),
            "variables": entry["variables"],
        }
        for entry in services_info
        for definition in (entry["definition"],)
    ]

    # The help text of a service does NOT go through the server: the existing page opens
    # it in a dialog (openServiceHelp() in static/js/wizard.js, which fetches the text at
    # /forms/wizard/help/<template>).
    return {
        "navigation": get_navigation(user, current_path="/services"),
        "services": services,
    }


def build_lotc_introduction(user: dict[str, Any] | None) -> dict[str, Any]:
    """The introduction page: the services ZAD offers, straight from the catalogue.

    The list is not retyped: it comes from ``SERVICE_DEFINITIONS``, the same source the
    wizard and the services overview draw on, so no service can appear that does not
    exist. The description is copied LITERALLY, so the page cannot promise something
    other than the service itself says.

    Split into two lists, because that is the difference the page tells:

    - ``diensten_zelf``: what you can switch ON for your project.
    - ``diensten_achtergrond``: the system services (``kind=SYSTEM``), which always run
      and are not selectable.

    A service that is replaced on this cluster (``replaced_on_cluster``, the same question
    the service picker asks) and a service with ``hidden=True`` are both left out: neither
    is a choice for whoever starts here.
    """
    from opi.http.render.navigation_lotc import get_navigation
    from opi.services.registry import SERVICE_DEFINITIONS, get_service
    from opi.services.services_enums import ServiceKind
    from opi.utils.nldd_iconen import to_nldd_icon

    selectable: list[dict[str, str]] = []
    background: list[dict[str, str]] = []
    for service_type, definition in SERVICE_DEFINITIONS.items():
        if definition.hidden or get_service(service_type).replaced_on_cluster(settings.CLUSTER_MANAGER):
            continue
        card = {
            "name": service_type.value,
            "label": definition.name,
            "summary": definition.description,
            "icon": to_nldd_icon(definition.icon),
            "color": definition.color,
        }
        if definition.kind is ServiceKind.SYSTEM:
            background.append(card)
        else:
            selectable.append(card)

    return {
        "navigation": get_navigation(user, current_path="/introductie"),
        "diensten_zelf": selectable,
        "diensten_achtergrond": background,
    }


def build_lotc_dashboard(*, user: dict[str, Any] | None, **_unused: Any) -> dict[str, Any]:  # noqa: ANN401  # extra context a caller may pass
    """What the dashboard needs on top of its own data: only the navigation.

    The route supplies everything else and the template reads exactly those keys;
    reshaping them here would give a second shape of the same data.
    """
    from opi.http.render.navigation_lotc import get_navigation

    return {"navigation": get_navigation(user, current_path="/dashboard")}


def build_lotc_admin(*, user: dict[str, Any] | None, current_path: str) -> dict[str, Any]:
    """What an admin page needs on top of its own data: only the navigation.

    One function for all four admin pages, because they need the same thing.
    ``current_path`` decides which item in the sidebar is active and does differ per page.
    """
    from opi.http.render.navigation_lotc import get_navigation

    return {"navigation": get_navigation(user, current_path=current_path)}


#: Sort value for a project whose edit time is unknown, which makes it the oldest.
#:
#: Unreachable in practice: every project file has at least the commit that created it. If
#: the date is missing anyway, git cannot be read at all and it is missing for every
#: project, so the relative order does not matter.
_UNKNOWN_EDITED = datetime.min.replace(tzinfo=UTC)


def _by_edited(project: dict[str, Any]) -> datetime:
    return project.get("last_modified_at") or _UNKNOWN_EDITED


#: What the project list can be sorted on: key in the URL (``?sort=``), label in the menu,
#: sort key, and whether it is reversed. A list and not a dict, because the ORDER is the
#: order in the menu.
PROJECT_SORT_ORDERS: list[tuple[str, str, Any, bool]] = [
    ("naam", "Naam (A-Z)", lambda p: (p["display_name"] or p["name"]).lower(), False),
    ("naam-af", "Naam (Z-A)", lambda p: (p["display_name"] or p["name"]).lower(), True),
    ("bewerkt", "Laatst bewerkt (nieuwste eerst)", _by_edited, True),
    ("bewerkt-op", "Laatst bewerkt (oudste eerst)", _by_edited, False),
]

#: Dutch month abbreviations, with index 0 unused so that month number 1 lands on 'jan'.
_MONTHS = ("", "jan", "feb", "mrt", "apr", "mei", "jun", "jul", "aug", "sep", "okt", "nov", "dec")

#: Beyond this, "how long ago" says nothing the date does not already say.
_RELATIVE_UP_TO_DAYS = 30


def relative_time(elapsed: timedelta) -> str:
    """A duration in words, as the project list shows it.

    Rounded DOWN, because that is what a reader expects: 119 minutes old reads as one hour
    and not as two. A negative duration (a timestamp from the future, clock skew between
    the committer and us) reads as the shortest form instead of an impossible number.
    """
    seconds = int(elapsed.total_seconds())
    if seconds < 60:
        return "zojuist"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minuut geleden" if minutes == 1 else f"{minutes} minuten geleden"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} uur geleden"
    days = hours // 24
    return "gisteren" if days == 1 else f"{days} dagen geleden"


def read_timestamp(timestamp: str | None) -> datetime | None:
    """An ISO 8601 timestamp from git as a datetime, or None when it is unusable.

    Always with a timezone, so that a naive is never compared with an aware one: that is a
    TypeError, and it would take down the whole page over a detail at the end of a row.
    """
    if not timestamp:
        return None
    try:
        edited = datetime.fromisoformat(timestamp)
    except ValueError:
        logger.debug("Onleesbaar tijdstempel voor laatst-bewerkt: %r", timestamp)
        return None
    return edited if edited.tzinfo else edited.replace(tzinfo=UTC)


def last_edited(timestamp: str | None, *, now: datetime | None = None) -> dict[str, str] | None:
    """A project's 'last edited', ready to show.

    Gives ``label`` (what it says) and ``titel`` (the exact moment, for the tooltip), or
    None when there is no usable timestamp: the list then shows nothing instead of a dash
    that asks for an explanation.

    The label carries the date AND how long ago that was, because those answer different
    questions. Beyond a month the relative part disappears.
    """
    edited = read_timestamp(timestamp)
    if edited is None:
        return None

    now = now or datetime.now(UTC)
    elapsed = now - edited
    local = edited.astimezone()
    date_text = f"{local.day} {_MONTHS[local.month]} {local.year}"

    label = date_text if elapsed > timedelta(days=_RELATIVE_UP_TO_DAYS) else f"{date_text}, {relative_time(elapsed)}"
    return {"label": label, "titel": local.strftime("%d-%m-%Y %H:%M")}


def build_lotc_projects(
    request: Request,
    *,
    user: dict[str, Any] | None,
    projects: list[dict[str, Any]],
    last_modified: dict[str, str] | None = None,
) -> dict[str, Any]:
    """The real project list, in the shape the redrawn page reads.

    Searching and sorting happen HERE and not in the browser: it then works without
    JavaScript, a filtered list is shareable as a URL, and the count under the table keeps
    matching what is in it.
    """
    from opi.http.render.navigation_lotc import get_navigation

    return {
        "navigation": get_navigation(user, current_path="/projects"),
        **filter_lotc_projects(request, lotc_project_rows(projects, last_modified)),
    }


def lotc_project_rows(
    projects: list[dict[str, Any]],
    last_modified: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """The project data in the shape the table reads.

    Counting on the LISTS the route supplies, not on a ``deployment_count`` key: that one
    exists on the dashboard but not here.

    ``last_modified`` comes from the ProjectStore (git history) and is optional: when the
    route cannot fetch it, ``last_modified`` stays None and the list shows nothing.
    """
    edited = last_modified or {}
    return [
        {
            "name": project["name"],
            "display_name": project.get("display_name") or project["name"],
            "description": project.get("description", ""),
            "users": project.get("users") or [],
            "services": project.get("services") or [],
            "clusters": project.get("clusters") or [],
            "deployment_count": len(project.get("deployments") or []),
            "last_modified": last_edited(edited.get(project["name"])),
            # The raw date alongside it, because sorting on the text "3 dagen geleden"
            # does not do what it says.
            "last_modified_at": read_timestamp(edited.get(project["name"])),
        }
        for project in projects
    ]


def filter_lotc_projects(request: Request, projects: list[dict[str, Any]]) -> dict[str, Any]:
    """Apply ``?q=`` and ``?sort=``, and supply what the list and its bar need.

    Apart from :func:`build_lotc_projects` because the htmx route that returns only the
    TABLE needs exactly this piece and not the navigation around it.
    """
    search_term = (request.query_params.get("q") or "").strip()
    if search_term:
        needle = search_term.lower()
        found = [
            project
            for project in projects
            if needle in project["name"].lower()
            or needle in project["display_name"].lower()
            or needle in project["description"].lower()
        ]
    else:
        found = list(projects)

    chosen = request.query_params.get("sort") or PROJECT_SORT_ORDERS[0][0]
    key, reversed_order = next(
        ((s, r) for k, _, s, r in PROJECT_SORT_ORDERS if k == chosen),
        (PROJECT_SORT_ORDERS[0][2], PROJECT_SORT_ORDERS[0][3]),
    )
    found.sort(key=key, reverse=reversed_order)

    return {
        "projects": found,
        # The totals at the bottom count over ALL projects of this user, not over what the
        # search field leaves: "Je projecten" should not drop because you type something.
        # Only "Totaal" above the table follows the search term.
        "projects_all": projects,
        "project_query": search_term,
        "project_sort": chosen,
        "project_sorteringen": [(key, label) for key, label, _, _ in PROJECT_SORT_ORDERS],
    }


#: What the deployment table can be sorted on. Same shape as :data:`PROJECT_SORT_ORDERS`,
#: key in the URL (``?dsort=cluster``), label in the menu, sort key third, and a LIST for
#: the same reason: the order here is the order in the menu.
#:
#: There is no sort on STATUS. It is in the table (from the bundled query in
#: ``opi/services/argocd_overview.py``), but sorting on it would make the order depend on
#: another system's answer: the same URL would give a different order tomorrow.
DEPLOYMENT_SORT_ORDERS: list[tuple[str, str, Any]] = [
    ("naam", "Naam (A-Z)", lambda d: d["name"].lower()),
    ("naam-af", "Naam (Z-A)", lambda d: d["name"].lower()),
    ("cluster", "Cluster", lambda d: ((d.get("cluster") or "").lower(), d["name"].lower())),
    ("componenten", "Meeste componenten", lambda d: (-len(d.get("components") or []), d["name"].lower())),
]


def filter_lotc_deployments(
    request: Request, deployments: list[dict[str, Any]], deployment_open: str = ""
) -> dict[str, Any]:
    """Apply ``?q=`` and ``?dsort=`` to a project's deployments.

    Same setup as :func:`filter_lotc_projects`, and server-side for the same reason.

    ``dsort`` and not ``sort``: the project list already uses ``sort``, and both names can
    end up in one and the same URL as soon as someone shares a link. The SEARCH field is
    plain ``q``, because those two lists never sit on the same page.

    WHICH deployment is open is not decided here: the route does that, from the PATH, with
    :func:`choose_deployment`, and it arrives as ``deployment_open``.
    """
    search_term = (request.query_params.get("q") or "").strip()
    if search_term:
        needle = search_term.lower()
        found = [
            deployment
            for deployment in deployments
            if needle in deployment["name"].lower() or needle in (deployment.get("cluster") or "").lower()
        ]
    else:
        found = list(deployments)

    # An unknown ``dsort`` falls back on the FIRST order, and that name goes back into the
    # page: leaving it as it came in made the hidden field and hx-push-url promise an
    # order that was not used.
    requested_sort = request.query_params.get("dsort") or ""
    known = {k for k, _, _ in DEPLOYMENT_SORT_ORDERS}
    chosen = requested_sort if requested_sort in known else DEPLOYMENT_SORT_ORDERS[0][0]
    key = next(s for k, _, s in DEPLOYMENT_SORT_ORDERS if k == chosen)
    found.sort(key=key)
    if chosen == "naam-af":
        found.reverse()

    opened = next((deployment for deployment in deployments if deployment["name"] == deployment_open), None)

    return {
        "deployments_zichtbaar": found,
        # The count above the table follows the search term ("3 van 12"), which needs the
        # total over ALL deployments.
        "deployments_alle": deployments,
        "deployment_query": search_term,
        "deployment_sort": chosen,
        "deployment_sorteringen": [(key, label) for key, label, _ in DEPLOYMENT_SORT_ORDERS],
        "deployment_open": deployment_open,
        # The deployment itself, because the tabs render only ONE and need no loop to find
        # it. Empty when the project has none; the tab then shows "Nog geen deployments".
        "deployment_geopend": opened,
    }


def deployment_status_tags(
    *,
    replacing_badges: list[str],
    accompanying_badges: list[str],
    argocd: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """The status labels of ONE row in the deployment table: ``[{'label': .., 'type': ..}]``.

    Two sources that belong together in one column: what the SERVICES report (sleep mode,
    switched off), and the health and sync from ARGOCD out of the bundled query in
    ``opi/services/argocd_overview.py``.

    The rule by which they come together is that of the status card in the panel
    (RC-31/RC-35): a service badge takes the place of exactly ONE verdict, the green
    "Healthy" that yields zero replicas. Any other verdict was really observed by ArgoCD
    and keeps its badge, otherwise switching a component off hides a real outage.

    ``argocd`` is ``None`` when ArgoCD is not connected. Only the service badges then
    remain: the page says in plain words that the status is missing, and a made-up
    "Unknown" per row would make that unreadable.
    """
    tags: list[dict[str, str]] = [{"label": badge, "type": "info"} for badge in accompanying_badges]
    tags += [{"label": badge, "type": "info"} for badge in replacing_badges]

    if argocd is None:
        return tags

    if not argocd.get("available"):
        # ArgoCD does not know this application. That is not the same as "unhealthy":
        # nothing has been rolled out yet, or the rollout has not got through.
        return [*tags, {"label": "Niet in ArgoCD", "type": "default"}]

    health = argocd.get("health", "Unknown")
    sync = argocd.get("sync", "Unknown")

    if not (replacing_badges and health == "Healthy"):
        tags.append(
            {
                "label": health,
                "type": "success" if health == "Healthy" else "warning" if health == "Progressing" else "error",
            }
        )

    # A resource that is not rolled out because its kind is not on the allowlist. ArgoCD
    # keeps the app Synced and Healthy, so without this tag the row reads as plain green.
    excluded = argocd.get("excluded_resources") or 0
    if excluded:
        tags.append(
            {
                "label": f"{excluded} resource niet uitgerold"
                if excluded == 1
                else f"{excluded} resources niet uitgerold",
                "type": "warning",
            }
        )

    # Unknown means ArgoCD cannot compare desired and actual state (often a render or CMP
    # error), so red and not neutral grey, which would read as "still working on it".
    tags.append(
        {
            "label": sync,
            "type": "success"
            if sync == "Synced"
            else "warning"
            if sync == "OutOfSync"
            else "error"
            if sync == "Unknown"
            else "default",
        }
    )
    return tags


def build_deployment_status_column(
    deployments: list[dict[str, Any]],
    deployment_states: dict[str, Any],
    argocd_statuses: dict[str, dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    """The status column of the whole table: deployment name -> labels.

    A dict and not a computation in the template, so that the rule from
    :func:`deployment_status_tags` sits in ONE place and can be tested without rendering a
    page.
    """
    return {
        name: deployment_status_tags(
            replacing_badges=state.replacing_badges if state else [],
            accompanying_badges=state.accompanying_badges if state else [],
            argocd=argocd_statuses.get(name) if argocd_statuses else None,
        )
        for deployment in deployments
        for name in (deployment["name"],)
        for state in (deployment_states.get(name),)
    }


#: The tabs of the project page.
#:
#: Every tab has its OWN PATH, no ``?tab=`` parameter: a tab is another page about the
#: same project, and a path says that where a query parameter does not. The project name
#: comes first and the tab behind it: ``/projects/<name>/deployments``. The old shape
#: (``/projects/deployments/<name>``) redirects, so shared links keep working.
#:
#: ``path`` sits next to the label because the address is NOT derivable from the key:
#: Overzicht is called ``project`` and lives at ``details``.
#:
#: Services and Services info are two things (RC-101). Services is about MANAGEMENT: which
#: services are on, what they do, how they are bound. Services info carries the blocks the
#: SERVICES themselves supply through detail_page_sections. See
#: features/services-info-tabblad.md for the trade-off.
PROJECT_TABS = {
    "project": {"label": "Overzicht", "path": "details", "icon": "house"},
    "team": {"label": "Team", "path": "team", "icon": "person-2"},
    "componenten": {"label": "Componenten", "path": "componenten", "icon": "blocks-9"},
    "services": {"label": "Services", "path": "services", "icon": "puzzle-piece"},
    "services-info": {"label": "Services info", "path": "services-info", "icon": "info-circle"},
    "deployments": {"label": "Deployments", "path": "deployments", "icon": "globe-rack-server"},
    "metrics": {"label": "Metrics", "path": "metrics", "icon": "chart-x-y-axis-line"},
    "backups": {"label": "Backups", "path": "backups", "icon": "backup-in-cloud"},
    "taken": {"label": "Taken", "path": "taken", "icon": "check-list"},
    # The map of this project. The tab carries only the element; the picture comes from
    # /projects/<name>/kaart.json.
    "kaart": {"label": "Kaart", "path": "kaart", "icon": "centralized-network"},
    # Behind Action.AUDIT_READ, so not in the bar for every member; the route decides.
    "audit": {"label": "Audit", "path": "audit", "icon": "clock-arrow-counter-clockwise"},
}

#: The tab an unknown (or missing) tab ends up on.
DEFAULT_TAB = next(iter(PROJECT_TABS))

#: The tabs that only exist when they have something to show (RC-101).
#:
#: Services info carries the blocks the SERVICES supply, so a project that uses no service
#: with such a block has nothing there. Which tabs those are sits here and not in the
#: template, so that the tab bar and the route (which redirects away from such an empty
#: tab) talk about the same rule.
CONDITIONAL_TABS = ("services-info",)

#: The tabs that show ONE deployment at a time, and therefore carry it in their PATH:
#: ``/projects/<project>/deployments/<deployment>``.
#:
#: The server renders one and the URL says which: that is shareable, the back button
#: works, and the choice travels along to the other tab because the tab bar carries it in
#: its addresses.
TABS_WITH_DEPLOYMENT = ("deployments", "metrics", "backups")


def project_tab_url(project_name: str, tab: str, query: str = "", deployment: str = "") -> str:
    """The address of a project's tab.

    Computed in one place, so that the tab bar, the breadcrumbs, the key figures and the
    router talk about the same address. An unknown tab falls back on Overzicht instead of
    inventing a path that has no route.

    ``deployment`` is appended as an extra segment, but only on the tabs that show one at
    a time (:data:`TABS_WITH_DEPLOYMENT`). On Overzicht that path would have no route.
    """
    path = PROJECT_TABS.get(tab, PROJECT_TABS[DEFAULT_TAB])["path"]
    address = f"/projects/{project_name}/{path}"
    if deployment and tab in TABS_WITH_DEPLOYMENT:
        address += f"/{quote(deployment, safe='')}"
    return address + (f"?{query}" if query else "")


def choose_deployment(names: list[str], *preferences: str) -> str:
    """Which deployment is open: the first preference that really exists.

    A name that yields nothing (a deleted deployment in a shared link) falls back on the
    first by name, because that is how they sit in the picker. A project without
    deployments yields an empty name.

    This is the ONE rule for "which one is open". It used to sit half in the server
    (``?deployment=``) and half in the browser (a remembered choice in sessionStorage),
    and those two could overwrite each other.
    """
    for preference in preferences:
        if preference and preference in names:
            return preference
    return sorted(names)[0] if names else ""


def deployment_page_address(request: Request, project_name: str, deployment_open: str) -> str:
    """The address where THIS page belongs, with the deployment in the path.

    The querystring travels along except ``deployment=``: that old shape becomes a
    redirect to the path, so that there is one address per deployment and not two.
    """
    tab = tab_from_path(request.url.path)
    remaining = [(key, value) for key, value in request.query_params.multi_items() if key != "deployment"]
    return project_tab_url(project_name, tab, urlencode(remaining), deployment=deployment_open)


def tab_from_path(path: str) -> str:
    """Which tab belongs to this path? ``/projects/<name>/deployments`` -> ``deployments``.

    Reads the THIRD segment (the tab sits behind the project name) and not the route's
    name, so that a path that does not belong ends up on Overzicht instead of erroring.
    """
    segments = path.strip("/").split("/")
    segment = segments[2] if len(segments) > 2 else ""
    return next((tab for tab, data in PROJECT_TABS.items() if data["path"] == segment), DEFAULT_TAB)


def build_lotc_project_details(
    request: Request,
    *,
    user: dict[str, Any] | None,
    project: dict[str, Any],
    deployment_open: str = "",
    empty_tabs: tuple[str, ...] = (),
) -> dict[str, Any]:
    """The real project data, in the shape the tabbed page reads.

    Nothing is converted: ``project`` already has the shape the route builds and the
    template reads exactly those keys. What happens here is determining the navigation and
    the active tab, which comes from the PATH and no longer from ``?tab=``.

    Resource usage is NOT in here. The existing page loads that separately with htmx, so
    that a slow Prometheus does not hold up the page.

    ``empty_tabs`` are the conditional tabs (:data:`CONDITIONAL_TABS`) that have nothing to
    show for THIS project; they stay out of the bar. The route decides that, because that
    is where it is known what the services supply.
    """
    from opi.http.render.navigation_lotc import get_navigation

    active_tab = tab_from_path(request.url.path)
    return {
        "navigation": get_navigation(user, current_path="/projects"),
        # The chosen deployment travels along in the tab addresses, so it stays put when
        # you switch tabs: a plain link to an address that already carries the name.
        "tabs": {
            tab: {**data, "url": project_tab_url(project["name"], tab, deployment=deployment_open)}
            for tab, data in PROJECT_TABS.items()
            if tab not in empty_tabs
        },
        "active_tab": active_tab,
        "project": project,
        **filter_lotc_deployments(request, project.get("deployments") or [], deployment_open),
    }


#: Suffixes of a hostname that only exists INSIDE the cluster. A link there opens in no
#: browser, so the page then shows the SHA as text.
_INTERNAL_HOSTS = (".svc.cluster.local", ".cluster.local")


def commit_address(sha: str) -> str:
    """The web address of a commit in the projects repo, or empty when there is none.

    ``GIT_PROJECTS_WEB_URL`` wins: the clone URL is a cluster address in the sandbox.
    Without that setting the derivation from the clone URL remains, because on GitHub and
    Forgejo that is the same address with ``/commit/<sha>`` behind it. A ``git://`` or
    ``ssh://`` URL has no page to point at.
    """
    web = settings.GIT_PROJECTS_WEB_URL.strip()
    base = web or settings.GIT_PROJECTS_SERVER_URL
    if not base.startswith(("http://", "https://")):
        return ""
    if not web and (urlparse(base).hostname or "").endswith(_INTERNAL_HOSTS):
        return ""
    return f"{base.removesuffix('.git').rstrip('/')}/commit/{quote(sha, safe='')}"


def audit_limit(request: Request) -> int:
    """How many audit rows this page asks for: ``?limit=``, with a ceiling.

    Too large a value yields the ceiling and not an error: this is a page and not a
    contract.
    """
    requested = request.query_params.get("limit")
    try:
        count = int(requested) if requested else LIST_DEFAULT_LIMIT
    except ValueError:
        return LIST_DEFAULT_LIMIT
    return max(1, min(count, LIST_MAX_LIMIT))


#: The zone the audit computes and shows in. A constant per module, as the schedulers
#: already do: there is no shared zone constant in this codebase.
AUDIT_TZ = ZoneInfo("Europe/Amsterdam")

#: How many characters of the search term the database sees. A longer term finds nothing a
#: shorter one does not also find, and does not belong in an ILIKE pattern or in the
#: address bar.
AUDIT_QUERY_MAX = 200


@dataclass(frozen=True, slots=True)
class AuditFilters:
    """What ``?q=``, ``?from=``, ``?until=``, ``?before=``, ``?after=`` and ``?limit=`` say.

    The ``_text`` fields are the values as they go back into the page: the search field,
    the date fields and every paging link carry them on unchanged. An unreadable value is
    not in there, just as :func:`audit_limit` gives no error but falls back.
    """

    query: str = ""
    since: datetime | None = None
    until: datetime | None = None
    before: AuditCursor | None = None
    after: AuditCursor | None = None
    since_text: str = ""
    until_text: str = ""
    limit_text: str = ""

    @property
    def active(self) -> bool:
        """Whether anything is being filtered; paging is not a filter."""
        return bool(self.query or self.since or self.until)


def _audit_day_start(value: str, *, next_day: bool = False) -> tuple[datetime, str] | None:
    """``yyyy-mm-dd`` as the start of that day (or the day after) in Europe/Amsterdam."""
    try:
        day = date.fromisoformat(value)
        limit = day + timedelta(days=1) if next_day else day
    except ValueError, OverflowError:
        # OverflowError: ``9999-12-31`` reads fine, but the day after does not exist.
        return None
    return datetime.combine(limit, time.min, tzinfo=AUDIT_TZ), day.isoformat()


def _audit_cursor(value: str) -> AuditCursor | None:
    """``<iso-time>_<uuid>`` back into a cursor; unreadable is no cursor."""
    moment_text, _, key = value.rpartition("_")
    if not moment_text:
        return None
    try:
        moment = datetime.fromisoformat(moment_text)
        row = uuid.UUID(key)
    except ValueError:
        return None
    # A naive moment does not come from us: the cursor is made out of a timestamptz.
    return None if moment.tzinfo is None else AuditCursor(time=moment, id=row)


def audit_filters(request: Request) -> AuditFilters:
    """The audit filters from the querystring, without ever erroring.

    An unreadable date or cursor FALLS AWAY instead of yielding a 400 or a 500, the same
    choice as in :func:`audit_limit`: this is a page and not a contract.

    ``from`` after ``until`` is not swapped: that yields an empty answer in the query, and
    that is exactly what the question says.

    ``before`` and ``after`` together is a programming error for
    :func:`~opi.services.audit.list_actions`. A querystring can carry both, so ``before``
    wins here and ``after`` falls away: older is the direction in which you read a history.
    """
    parameters = request.query_params
    since = _audit_day_start(parameters.get("from") or "")
    until = _audit_day_start(parameters.get("until") or "", next_day=True)
    before = _audit_cursor(parameters.get("before") or "")
    after = None if before else _audit_cursor(parameters.get("after") or "")
    return AuditFilters(
        query=(parameters.get("q") or "").strip()[:AUDIT_QUERY_MAX],
        since=since[0] if since else None,
        until=until[0] if until else None,
        before=before,
        after=after,
        since_text=since[1] if since else "",
        until_text=until[1] if until else "",
        limit_text=parameters.get("limit") or "",
    )


def audit_cursor_text(row: Any) -> str:  # noqa: ANN401  # a database row
    """A row's cursor, as it appears in a paging link."""
    return f"{row.time.isoformat()}_{row.id}"


def audit_url(base: str, filters: AuditFilters, *, before: str = "", after: str = "") -> str:
    """The audit tab with the active filters, and at most one cursor.

    Paging keeps the search term, the period and ``limit``; a new search or period comes
    from the form and therefore carries no cursor any more.
    """
    values = [
        ("q", filters.query),
        ("from", filters.since_text),
        ("until", filters.until_text),
        ("limit", filters.limit_text),
        ("before", before),
        ("after", after),
    ]
    querystring = urlencode([(name, value) for name, value in values if value])
    return f"{base}?{querystring}" if querystring else base


def build_lotc_audit(rows: list[Any]) -> list[dict[str, Any]]:
    """A project's audit rows in the shape the tab reads.

    ``origin`` is NOT in it: that column is for platform admins per the events design, and
    they read it in the database.
    """
    shown = []
    for row in rows:
        data = row.data or {}
        shown.append(
            {
                # In Dutch time, because the period filter computes in that zone: an
                # action at 00:30 here sat in the table as UTC on the day before.
                "time": row.time.astimezone(AUDIT_TZ) if row.time.tzinfo else row.time,
                "actor": row.actor or "",
                "actor_kind": row.actor_kind,
                "action": data.get("action", ""),
                "subject": row.subject or "",
                "allowed": bool(data.get("allowed")),
                "cause": data.get("cause") or "",
                "commits": [
                    {"sha": sha, "short": sha[:8], "url": commit_address(sha)} for sha in data.get("commit_shas") or []
                ],
            }
        )
    return shown


async def audit_page(request: Request, project_name: str) -> dict[str, Any]:
    """This project's audit rows plus the search field, the period and the paging links.

    ``project_name`` comes from the PATH, the same name the route gate decided about:
    ``list_actions`` checks nothing itself, so a name from the querystring would let the
    gate and the read go over two different projects.
    """
    filters = audit_filters(request)
    limit = audit_limit(request)
    base = project_tab_url(project_name, "audit")
    context: dict[str, Any] = {
        "audit_rijen": [],
        "audit_fout": False,
        "audit_url": base,
        "audit_query": filters.query,
        "audit_van": filters.since_text,
        "audit_tot": filters.until_text,
        "audit_limit_text": filters.limit_text,
        "audit_filter_actief": filters.active,
        "audit_wis_url": audit_url(base, AuditFilters(limit_text=filters.limit_text)),
        "audit_ouder_url": "",
        "audit_nieuwer_url": "",
    }
    try:
        # One row more than the page shows: that one reveals whether there is another page.
        read = await list_actions(
            project_name,
            limit=limit + 1,
            query=filters.query or None,
            since=filters.since,
            until=filters.until,
            before=filters.before,
            after=filters.after,
        )
    except SQLAlchemyError, OSError:
        # Same choice as with the rollout message on the project page: a database that
        # does not answer may not take the page with it. With a message on the tab,
        # because "no rows" would be an untrue answer here.
        logger.exception("De audit van project %s kon niet worden gelezen", project_name)
        context["audit_fout"] = True
        return context

    extra = len(read) > limit
    # Read ascending on ``after``, so the extra row is the NEWER one there. That one falls
    # away, otherwise there is a gap between this page and the page you left.
    read = read[-limit:] if filters.after is not None else read[:limit]
    context["audit_rijen"] = build_lotc_audit(read)
    if read:
        # Older exists when there are older rows still; after a step to newer that is the
        # page you came from. Newer is there as soon as you did not start on the newest
        # page, even when that step yields nothing more.
        if extra or filters.after is not None:
            context["audit_ouder_url"] = audit_url(base, filters, before=audit_cursor_text(read[-1]))
        if filters.before is not None or filters.after is not None:
            context["audit_nieuwer_url"] = audit_url(base, filters, after=audit_cursor_text(read[0]))
    return context


#: The cookie the scheme choice (light/dark) is kept in.
#:
#: Remembered server-side instead of in localStorage, which also saves the flash you get
#: when JavaScript sets the theme only after the first paint.
SCHEME_COOKIE = "zad_scheme"

#: The three settings. "" is system: the page then sets no data-scheme and NLDD follows
#: the operating system's preference.
SCHEMES = ("", "light", "dark")
